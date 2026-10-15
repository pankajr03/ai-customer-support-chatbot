"""The customer chat API contract (Stage 7): POST /api/v1/conversations/{id}/chat.

This is the stable, client-facing shape of one chat turn. It is deliberately separate
from the internal pipeline types (the Stage 4 ``Decision``, the Stage 5
``GenerationOutcome``), so the pipeline can change without changing what clients see:

- ``ChatDecision`` is the decision clients branch on. Internal values are mapped to it
  explicitly (``chat_decision``, ``decision_for_outcome``); there is no pass-through.
- ``CustomerChatResponse`` checks that the decision, the generation method and the
  sources agree (see ``_ALLOWED_METHODS``), so an infrastructure failure or a fallback
  can never be returned looking like a confident answer.
- Sources are ``ChatSourceReference`` objects built only from the approved FAQ evidence
  of this turn (or, for a replayed turn, from the approved knowledge base), never from
  model text.
- Response models reject unknown fields, so prompts, rewritten queries, model reasons
  and other diagnostics cannot be attached by accident.

Where the session and token go: the session ID is in the path and the access token in
the ``Authorization: Bearer`` header. Neither is ever part of a request or response body
here (the token is returned once, by ``POST /api/v1/conversations``).

Compatibility with Stage 6: the request still accepts ``category`` and the
``Idempotency-Key`` header; the response still carries ``user_message``,
``assistant_message`` and the same ``generation_method`` / ``usage`` fields. Those two
message objects are deprecated in favour of the top-level fields (see README, "Customer
chat contract").
"""

import re
import unicodedata
import uuid
from collections.abc import Iterable, Sequence
from enum import StrEnum
from typing import Annotated, Self

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)
from pydantic_core import PydanticCustomError

from app.models.answer import GenerationMethod, UsageSummary
from app.models.conversation import (
    MAX_USER_MESSAGE_LENGTH,
    ConversationMessage,
    ConversationMessageResponse,
    MessageRole,
)
from app.models.faq import Category, FAQId
from app.models.generation import GenerationOutcome, GenerationResult
from app.prompts.customer_support import MAX_SOURCES
from app.services.answer_decision_service import Decision


class ChatDecision(StrEnum):
    """What the assistant did with the customer's message."""

    ANSWERED = "answered"  # an approved or grounded answer, with its source FAQs
    NO_MATCH = "no_match"  # nothing reliable matched: a safe fallback message
    AMBIGUOUS = "ambiguous"  # several FAQs fit: a clarifying question
    RETRIEVAL_UNAVAILABLE = "retrieval_unavailable"  # search failed: a "try again" message


# Internal (Stage 4-6) decisions -> the public contract. Every internal value must be
# listed here; tests check that nothing is missing.
_DECISIONS: dict[Decision, ChatDecision] = {
    Decision.ANSWERED: ChatDecision.ANSWERED,
    Decision.NO_MATCH: ChatDecision.NO_MATCH,
    Decision.AMBIGUOUS: ChatDecision.AMBIGUOUS,
    Decision.RETRIEVAL_UNAVAILABLE: ChatDecision.RETRIEVAL_UNAVAILABLE,
}

# The decision that describes what the customer actually received. It usually equals
# Stage 4's decision, but not always: when Stage 4 answers with an FAQ that cannot be
# used, Stage 5 returns the no-match message, and that turn must be reported (and
# stored) as no_match, not answered.
_OUTCOME_DECISIONS: dict[GenerationOutcome, Decision] = {
    GenerationOutcome.GENERATED: Decision.ANSWERED,
    GenerationOutcome.APPROVED_ANSWER: Decision.ANSWERED,
    GenerationOutcome.CLARIFICATION: Decision.AMBIGUOUS,
    GenerationOutcome.NO_MATCH: Decision.NO_MATCH,
    GenerationOutcome.RETRIEVAL_UNAVAILABLE: Decision.RETRIEVAL_UNAVAILABLE,
}

# Which generation methods each decision may have. Only answered replies come from FAQ
# answers; everything else is a clarifying question or a configured safe message.
_ALLOWED_METHODS: dict[ChatDecision, frozenset[GenerationMethod]] = {
    ChatDecision.ANSWERED: frozenset(
        {GenerationMethod.LLM_GROUNDED, GenerationMethod.APPROVED_FAQ}
    ),
    ChatDecision.AMBIGUOUS: frozenset({GenerationMethod.CLARIFICATION}),
    ChatDecision.NO_MATCH: frozenset({GenerationMethod.FALLBACK}),
    ChatDecision.RETRIEVAL_UNAVAILABLE: frozenset({GenerationMethod.FALLBACK}),
}
# Decisions whose reply is not based on any FAQ, so they never list sources.
_SOURCELESS = frozenset({ChatDecision.NO_MATCH, ChatDecision.RETRIEVAL_UNAVAILABLE})


def reply_problem(
    decision: Decision | ChatDecision, method: GenerationMethod, source_ids: Sequence[str]
) -> str | None:
    """Why a reply's decision, generation method and sources disagree, or None if they
    agree. A fallback must never look like an answer, nor an answer like a fallback."""
    public = ChatDecision(decision.value)
    if method not in _ALLOWED_METHODS[public]:
        return f"generation_method {method} is not valid for {public}"
    if len(set(source_ids)) != len(source_ids):
        return "sources must not contain duplicates"
    if public in _SOURCELESS and source_ids:
        return f"{public} replies cannot have sources"
    if public is ChatDecision.ANSWERED and not source_ids:
        return "answered replies must be based on at least one FAQ"
    return None


def chat_decision(decision: Decision) -> ChatDecision:
    """The public decision for an internal one."""
    return _DECISIONS[decision]


def decision_for_outcome(result: GenerationResult) -> Decision:
    """The decision matching what the customer received (see ``_OUTCOME_DECISIONS``)."""
    return _OUTCOME_DECISIONS[result.outcome]


def default_generation_method(decision: Decision) -> GenerationMethod:
    """How a reply was most plausibly produced, for stored replies from before
    ``generation_method`` was recorded. Never claims an LLM was used."""
    if decision is Decision.ANSWERED:
        return GenerationMethod.APPROVED_FAQ
    if decision is Decision.AMBIGUOUS:
        return GenerationMethod.CLARIFICATION
    return GenerationMethod.FALLBACK


# --- Request --------------------------------------------------------------------------

_EXAMPLE_CLIENT_MESSAGE_ID = "4c981eb1-7847-41c0-bf6b-0a0e1b9d6d91"
# Invisible characters that carry no meaning at the start or end of a message (zero-width
# space, non-joiner and joiner, word joiner, byte-order mark). Inside a message they are
# kept: joiners are part of emoji sequences and some scripts.
_INVISIBLE = frozenset("\u200b\u200c\u200d\u2060\ufeff")
# Allowed despite being control characters: tab and line breaks.
_ALLOWED_CONTROLS = frozenset("\t\n\r")
# The canonical UUID text form only: no braces, "urn:uuid:" prefix or missing hyphens.
_UUID_TEXT = re.compile(r"^[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$")


def _is_edge_noise(character: str) -> bool:
    return character.isspace() or character in _INVISIBLE


def strip_message(value: object) -> object:
    """Remove whitespace and invisible characters around a message, nothing else.

    Inner spacing, line breaks, punctuation, emoji and every script are kept exactly as
    sent: they can carry meaning (a list, an order number, a name).
    """
    if not isinstance(value, str):
        return value  # rejected as "string_type" by the str validation that follows
    start, end = 0, len(value)
    while start < end and _is_edge_noise(value[start]):
        start += 1
    while end > start and _is_edge_noise(value[end - 1]):
        end -= 1
    return value[start:end]


def _check_message(value: str) -> str:
    if not value:
        raise PydanticCustomError("blank_message", "Message must not be empty or only whitespace")
    # Control characters (NUL, escape, bell, ...): never part of a customer's text.
    # Unpaired surrogates are already rejected by the str validation ("string_unicode").
    for character in value:
        if unicodedata.category(character) == "Cc" and character not in _ALLOWED_CONTROLS:
            raise PydanticCustomError(
                "invalid_characters",
                "Message must not contain control characters other than tabs and line breaks",
            )
    return value


# The ceiling here is the storage limit; the configured limit
# (MAX_CUSTOMER_MESSAGE_CHARACTERS, default 4,000) is checked by the chat service.
CustomerMessageText = Annotated[
    str,
    StringConstraints(max_length=MAX_USER_MESSAGE_LENGTH),  # checked after stripping
    BeforeValidator(strip_message),
    AfterValidator(_check_message),
]


def _check_uuid_text(value: object) -> object:
    if isinstance(value, str) and not _UUID_TEXT.match(value):
        raise PydanticCustomError(
            "uuid_format",
            "client_message_id must be a UUID such as 4c981eb1-7847-41c0-bf6b-0a0e1b9d6d91",
        )
    return value


def _reject_nil(value: uuid.UUID) -> uuid.UUID:
    if value.int == 0 or value.int == (1 << 128) - 1:  # 0000... / ffff...: not unique
        raise PydanticCustomError("uuid_reserved", "client_message_id must not be the nil UUID")
    return value


ClientMessageId = Annotated[
    uuid.UUID, BeforeValidator(_check_uuid_text), AfterValidator(_reject_nil)
]


class CustomerChatRequest(BaseModel):
    """A customer message to add to a conversation.

    Only the text (and optional retry key and category) is accepted: the role, IDs and
    timestamps are set by the server, and unknown fields such as ``role``, ``history``,
    ``session_id`` or ``access_token`` are rejected.
    """

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "message": "Can I return my order?",
                    "client_message_id": _EXAMPLE_CLIENT_MESSAGE_ID,
                }
            ]
        },
    )

    message: CustomerMessageText = Field(
        description="The customer's message. Whitespace around it is removed; everything "
        "else (line breaks, punctuation, emoji, any script) is kept. Must not be blank or "
        "contain control characters other than tabs and line breaks. At most "
        "MAX_CUSTOMER_MESSAGE_CHARACTERS characters (default 4000; the schema shows the "
        f"{MAX_USER_MESSAGE_LENGTH}-character ceiling)."
    )
    client_message_id: ClientMessageId | None = Field(
        default=None,
        description="Optional client-generated UUID for this message (e.g. from "
        "crypto.randomUUID()), sent again unchanged when retrying. A repeat returns the "
        "stored result instead of answering twice; reusing it for a different message is "
        "a 422. Equivalent to the Idempotency-Key header; if both are sent they must match.",
    )
    category: Category | None = Field(
        default=None, description="Only use FAQs in this category (case-insensitive)."
    )

    @property
    def idempotency_key(self) -> str | None:
        """The canonical (lowercase, hyphenated) form, used as the stored retry key."""
        return str(self.client_message_id) if self.client_message_id is not None else None


# --- Response -------------------------------------------------------------------------


class ChatSourceReference(BaseModel):
    """An approved FAQ the reply is based on (for a clarification: one it offers)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    faq_id: FAQId = Field(description="The FAQ's ID, e.g. faq_001.")
    question: str = Field(min_length=1, description="The FAQ's question, as approved.")


class ChatUsageMetadata(UsageSummary):
    """LLM token usage for this reply, as reported by the API.

    The object is ``null`` when no LLM response was received (generation off, failed
    before a response, or a replayed turn), which is different from a reported 0. A
    field inside it is ``null`` when the API did not report that number.
    """

    model_config = ConfigDict(extra="forbid")

    @classmethod
    def from_summary(cls, summary: UsageSummary | None) -> Self | None:
        return None if summary is None else cls.model_validate(summary.model_dump())


_EXAMPLE_SESSION_ID = "3f2b8c1e-7a4d-4e9b-9c61-2d5f0a8e4b17"
_EXAMPLE_MESSAGE_ID = "b7d13f08-62ac-4f5e-a9d4-81e6c3b2f950"
_EXAMPLE_ANSWER = (
    "Most unused items can be returned within 30 days of delivery. Start a return from "
    "Orders in your account."
)
# Illustrative only: real responses carry the actual answer, FAQs, IDs and usage.
_EXAMPLE_RESPONSE = {
    "session_id": _EXAMPLE_SESSION_ID,
    "message_id": _EXAMPLE_MESSAGE_ID,
    "client_message_id": _EXAMPLE_CLIENT_MESSAGE_ID,
    "request_id": "6f1c0e3a9b2d4c7e8a5f1b3d2c4e6a8b",
    "decision": "answered",
    "answer": _EXAMPLE_ANSWER,
    "generation_method": "llm_grounded",
    "sources": [{"faq_id": "faq_006", "question": "What is your return policy?"}],
    "usage": {
        "input_tokens": 850,
        "output_tokens": 70,
        "total_tokens": 920,
        "estimated_cost_usd": None,
    },
    "created_at": "2026-10-15T09:30:06Z",
    "user_message": {
        "message_id": "0c9e4a52-1b6f-4d8a-8e37-5a2c9f71d0b4",
        "role": "user",
        "content": "Can I return my order?",
        "created_at": "2026-10-15T09:30:05Z",
        "decision": None,
        "source_faq_ids": [],
    },
    "assistant_message": {
        "message_id": _EXAMPLE_MESSAGE_ID,
        "role": "assistant",
        "content": _EXAMPLE_ANSWER,
        "created_at": "2026-10-15T09:30:06Z",
        "decision": "answered",
        "source_faq_ids": ["faq_006"],
    },
}
_DEPRECATED = {"deprecated": True}


class CustomerChatResponse(BaseModel):
    """The assistant's reply to one customer message, and how it was produced."""

    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [_EXAMPLE_RESPONSE]})

    session_id: uuid.UUID = Field(description="The conversation (as in the path).")
    message_id: uuid.UUID = Field(
        description="ID of the stored assistant reply (as in GET .../messages)."
    )
    client_message_id: uuid.UUID | None = Field(
        description="The request's client_message_id (or a UUID Idempotency-Key); null "
        "when none was sent."
    )
    request_id: str | None = Field(
        description="ID of this HTTP request (also in the X-Request-ID header), for "
        "support. A replayed turn gets the ID of the retry, not of the original request."
    )
    decision: ChatDecision = Field(
        description="answered: an approved or grounded answer with its sources. no_match: "
        "a safe fallback message. ambiguous: a clarifying question. "
        "retrieval_unavailable: search is temporarily unavailable; try again later."
    )
    answer: str = Field(min_length=1, description="The text to show the customer.")
    generation_method: GenerationMethod = Field(
        description="llm_grounded: the LLM rewrote the approved FAQ answer and passed "
        "validation. approved_faq: the approved FAQ answer, verbatim. clarification: a "
        "clarifying question. fallback: a safe message (no_match, retrieval_unavailable)."
    )
    sources: list[ChatSourceReference] = Field(
        max_length=MAX_SOURCES,
        description="Approved FAQs the answer is based on (for ambiguous: the FAQs the "
        "question offers). Always empty for no_match and retrieval_unavailable.",
    )
    usage: ChatUsageMetadata | None = Field(
        description="Actual LLM token usage; null when no LLM response was received "
        "(not the same as 0)."
    )
    created_at: AwareDatetime = Field(description="When the reply was stored (UTC).")
    user_message: ConversationMessageResponse = Field(
        description="Deprecated (Stage 6 shape): the customer's stored message.",
        json_schema_extra=_DEPRECATED,
    )
    assistant_message: ConversationMessageResponse = Field(
        description="Deprecated (Stage 6 shape): the stored reply. Use the top-level "
        "fields instead.",
        json_schema_extra=_DEPRECATED,
    )

    @model_validator(mode="after")
    def _check_consistency(self) -> Self:
        stored = self.assistant_message.source_faq_ids
        ids = [source.faq_id for source in self.sources]
        # Checked against the stored sources: on a replay, withdrawn FAQs are left out of
        # ``sources`` but the reply was still based on them.
        problem = reply_problem(self.decision, self.generation_method, stored)
        if problem is None and len(set(ids)) != len(ids):
            problem = "sources must not contain duplicates"
        if problem is None and not set(ids) <= set(stored):
            problem = "sources must be among the stored reply's source FAQs"
        if problem is not None:
            raise ValueError(problem)
        if self.message_id != self.assistant_message.message_id:
            raise ValueError("message_id must be the assistant message's ID")
        return self

    @classmethod
    def from_records(
        cls,
        user: ConversationMessage,
        assistant: ConversationMessage,
        generation_method: GenerationMethod,
        *,
        sources: Iterable[ChatSourceReference] = (),
        usage: UsageSummary | None = None,
        request_id: str | None = None,
    ) -> Self:
        """Build the response from the two stored messages of one exchange.

        ``sources`` must come from approved FAQ evidence; their order follows the stored
        reply's ``source_faq_ids``.

        Raises:
            ValueError: wrong roles, different sessions, the reply does not directly
                follow the customer message, or the decision, method and sources disagree.
        """
        if user.role is not MessageRole.USER or assistant.role is not MessageRole.ASSISTANT:
            raise ValueError("expected a user message followed by an assistant message")
        if user.session_id != assistant.session_id:
            raise ValueError("messages belong to different sessions")
        if assistant.sequence != user.sequence + 1:
            raise ValueError("the assistant message must directly follow the user message")
        if assistant.decision is None:  # pragma: no cover - required for assistant messages
            raise ValueError("the assistant message has no decision")
        by_id = {source.faq_id: source for source in sources}
        ordered = [by_id[faq_id] for faq_id in assistant.source_faq_ids if faq_id in by_id]
        if len(ordered) != len(by_id):
            raise ValueError("sources must be among the stored reply's source FAQs")
        return cls(
            session_id=user.session_id,
            message_id=assistant.message_id,
            client_message_id=_as_uuid(user.idempotency_key),
            request_id=request_id,
            decision=chat_decision(assistant.decision),
            answer=assistant.content,
            generation_method=generation_method,
            sources=ordered,
            usage=ChatUsageMetadata.from_summary(usage),
            created_at=assistant.created_at,
            user_message=ConversationMessageResponse.model_validate(user),
            assistant_message=ConversationMessageResponse.model_validate(assistant),
        )


def _as_uuid(key: str | None) -> uuid.UUID | None:
    """The retry key as a UUID, or None (no key, or an Idempotency-Key that is not one)."""
    if key is None:
        return None
    try:
        return uuid.UUID(key)
    except ValueError:
        return None


def source_references(
    faq_ids: Sequence[str], questions: dict[str, str]
) -> list[ChatSourceReference]:
    """References for the FAQ IDs whose approved question is known, in order."""
    return [
        ChatSourceReference(faq_id=faq_id, question=questions[faq_id])
        for faq_id in faq_ids
        if faq_id in questions
    ]
