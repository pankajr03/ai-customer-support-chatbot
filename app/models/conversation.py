"""Conversation session and message models (Stage 6).

Two layers, kept separate on purpose:

- Storage records (``ConversationSession``, ``ConversationMessage``) are what a
  conversation store keeps. They are frozen and reject unknown fields, so nothing other
  than the listed fields (no prompts, model reasons, diagnostics or credentials) can be
  attached to them. Changes produce new copies (``touched``, ``with_status``).
- API models (``Conversation*Request`` / ``Conversation*Response``) are what clients send
  and receive. Responses are built from storage records and leave out internal details
  such as the per-session sequence number.

Identifiers are random UUID4s (122 random bits from the operating system's CSPRNG), so
they cannot be guessed from other IDs or from the time. All timestamps are timezone-aware
and stored in UTC; naive datetimes are rejected rather than assumed to be UTC.

Roles are ``user`` and ``assistant`` only: there is no ``system`` role, so a system
prompt can never be stored as a message. Clients cannot choose a role at all: a chat
request carries only the message text, and the server records it as ``user``.
"""

import uuid
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import (
    UUID4,
    AfterValidator,
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

from app.models.answer import GenerationMethod, UsageSummary
from app.models.faq import Category, FAQId
from app.models.semantic_search import SearchQueryText
from app.prompts.customer_support import MAX_SOURCES
from app.services.answer_decision_service import Decision
from app.services.semantic_search_service import MAX_QUERY_LENGTH

# A customer message is searched as a query, so it has the same limit.
MAX_USER_MESSAGE_LENGTH = MAX_QUERY_LENGTH
# Assistant replies may be an approved FAQ answer verbatim (up to 5000 characters) or a
# clarifying question listing several FAQ questions; this leaves room for both.
MAX_ASSISTANT_MESSAGE_LENGTH = 10_000


class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"


class SessionStatus(StrEnum):
    ACTIVE = "active"  # accepts new messages
    CLOSED = "closed"  # ended by the customer or an agent; history is read-only
    EXPIRED = "expired"  # passed expires_at; history is read-only


def new_id() -> uuid.UUID:
    """A new unpredictable identifier (UUID4, from ``os.urandom``)."""
    return uuid.uuid4()


def utc_now() -> datetime:
    return datetime.now(UTC)


def _to_utc(value: datetime) -> datetime:
    return value.astimezone(UTC)


def _strip(value: object) -> object:
    return value.strip() if isinstance(value, str) else value


def _require_text(value: str) -> str:
    if not value:
        raise ValueError("must not be empty or whitespace-only")
    return value


UTCDatetime = Annotated[AwareDatetime, AfterValidator(_to_utc)]
# A client-chosen key for safe retries, e.g. a UUID. Opaque to the server.
IDEMPOTENCY_KEY_PATTERN = r"^[A-Za-z0-9._:-]{1,64}$"
IdempotencyKey = Annotated[str, StringConstraints(pattern=IDEMPOTENCY_KEY_PATTERN)]
# Surrounding whitespace is removed; inner line breaks are kept (replies may contain lists).
MessageContent = Annotated[
    str,
    StringConstraints(max_length=MAX_ASSISTANT_MESSAGE_LENGTH),
    BeforeValidator(_strip),
    AfterValidator(_require_text),
]


# --- Storage records ------------------------------------------------------------------


class ConversationSession(BaseModel):
    """A stored conversation session. Create new sessions with ``ConversationSession.new``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: UUID4
    created_at: UTCDatetime
    updated_at: UTCDatetime
    expires_at: UTCDatetime | None = None  # None: the session does not expire
    status: SessionStatus = SessionStatus.ACTIVE

    @model_validator(mode="after")
    def _check_times(self) -> Self:
        if self.updated_at < self.created_at:
            raise ValueError("updated_at must not be before created_at")
        if self.expires_at is not None and self.expires_at <= self.created_at:
            raise ValueError("expires_at must be after created_at")
        return self

    @classmethod
    def new(cls, ttl: timedelta | None = None, now: datetime | None = None) -> Self:
        """A new active session with a random ID, expiring ``ttl`` after creation (if set)."""
        if ttl is not None and ttl <= timedelta(0):
            raise ValueError("ttl must be positive")
        created = now or utc_now()
        return cls(
            session_id=new_id(),
            created_at=created,
            updated_at=created,
            expires_at=created + ttl if ttl is not None else None,
        )

    def is_expired(self, now: datetime | None = None) -> bool:
        """Whether the session has expired, by status or by ``expires_at``."""
        if self.status is SessionStatus.EXPIRED:
            return True
        return self.expires_at is not None and (now or utc_now()) >= self.expires_at

    def accepts_messages(self, now: datetime | None = None) -> bool:
        return self.status is SessionStatus.ACTIVE and not self.is_expired(now)

    def touched(self, now: datetime | None = None) -> Self:
        """A copy with ``updated_at`` moved forward (never backward) to ``now``."""
        return self._replace(updated_at=max(self.updated_at, _to_utc(now or utc_now())))

    def with_status(self, status: SessionStatus, now: datetime | None = None) -> Self:
        """A copy with a new status; closed and expired sessions cannot be reopened."""
        if status is self.status:
            return self
        if self.status is not SessionStatus.ACTIVE:
            raise ValueError(f"cannot change a {self.status} session to {status}")
        return self.touched(now)._replace(status=status)

    def _replace(self, **changes: object) -> Self:
        # model_copy(update=...) skips validation; re-validate so invariants always hold.
        return type(self).model_validate({**self.model_dump(), **changes})


class ConversationMessage(BaseModel):
    """A stored message. Create messages with ``user_message`` / ``assistant_message``.

    ``sequence`` is the message's 1-based position in its session and defines the order;
    timestamps alone cannot, because two messages can share a timestamp. ``decision``,
    ``source_faq_ids`` and ``generation_method`` belong to assistant messages only;
    ``idempotency_key`` (the client's ``Idempotency-Key``) to user messages only.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    message_id: UUID4
    session_id: UUID4
    sequence: int = Field(ge=1, strict=True)
    role: MessageRole
    content: MessageContent
    created_at: UTCDatetime
    decision: Decision | None = None
    source_faq_ids: tuple[FAQId, ...] = Field(default=(), max_length=MAX_SOURCES)
    idempotency_key: IdempotencyKey | None = None
    generation_method: GenerationMethod | None = None

    @model_validator(mode="after")
    def _check_role_fields(self) -> Self:
        if self.role is MessageRole.USER:
            if len(self.content) > MAX_USER_MESSAGE_LENGTH:
                raise ValueError(
                    f"user message must be at most {MAX_USER_MESSAGE_LENGTH} characters"
                )
            if self.decision is not None or self.source_faq_ids or self.generation_method:
                raise ValueError(
                    "user messages cannot have a decision, source_faq_ids or generation_method"
                )
        else:
            if self.decision is None:
                raise ValueError("assistant messages must have a decision")
            if self.idempotency_key is not None:
                raise ValueError("assistant messages cannot have an idempotency_key")
        if len(set(self.source_faq_ids)) != len(self.source_faq_ids):
            raise ValueError("source_faq_ids must not contain duplicates")
        return self

    @classmethod
    def user_message(
        cls,
        session_id: uuid.UUID,
        sequence: int,
        content: str,
        now: datetime | None = None,
        idempotency_key: str | None = None,
    ) -> Self:
        """A customer message. The role is always ``user``, whatever the client sent."""
        return cls(
            message_id=new_id(),
            session_id=session_id,
            sequence=sequence,
            role=MessageRole.USER,
            content=content,
            created_at=now or utc_now(),
            idempotency_key=idempotency_key,
        )

    @classmethod
    def assistant_message(
        cls,
        session_id: uuid.UUID,
        sequence: int,
        content: str,
        decision: Decision,
        source_faq_ids: Iterable[str] = (),
        now: datetime | None = None,
        generation_method: GenerationMethod | None = None,
    ) -> Self:
        """A reply shown to the customer, with the decision and FAQs it is based on."""
        return cls(
            message_id=new_id(),
            session_id=session_id,
            sequence=sequence,
            role=MessageRole.ASSISTANT,
            content=content,
            created_at=now or utc_now(),
            decision=decision,
            source_faq_ids=tuple(source_faq_ids),
            generation_method=generation_method,
        )


def check_message_order(
    messages: Iterable[ConversationMessage], *, start: int = 1
) -> list[ConversationMessage]:
    """Return the messages of one session in order, or raise if the sequence is broken.

    ``start`` is the first expected position (1 for a whole conversation; later for a
    page of it).

    Raises:
        ValueError: messages from different sessions, or sequence numbers that are
            duplicated or not start, start + 1, ...
    """
    ordered = sorted(messages, key=lambda message: message.sequence)
    if len({message.session_id for message in ordered}) > 1:
        raise ValueError("messages belong to different sessions")
    if [message.sequence for message in ordered] != list(range(start, start + len(ordered))):
        raise ValueError("message sequence numbers must be consecutive without gaps")
    return ordered


# --- API models -----------------------------------------------------------------------

_EXAMPLE_SESSION_ID = "3f2b8c1e-7a4d-4e9b-9c61-2d5f0a8e4b17"
_EXAMPLE_CREATED = "2026-10-15T09:30:00Z"
_EXAMPLE_EXPIRES = "2026-10-15T10:00:00Z"
_EXAMPLE_USER_MESSAGE = {
    "message_id": "0c9e4a52-1b6f-4d8a-8e37-5a2c9f71d0b4",
    "role": "user",
    "content": "How do I reset my password?",
    "created_at": "2026-10-15T09:30:05Z",
    "decision": None,
    "source_faq_ids": [],
}
# Illustrative only: real responses carry the actual answer and FAQ IDs.
_EXAMPLE_ASSISTANT_MESSAGE = {
    "message_id": "b7d13f08-62ac-4f5e-a9d4-81e6c3b2f950",
    "role": "assistant",
    "content": "Select Sign In, then Forgot Password, and enter the email address on your "
    "account. We'll email you a link to set a new password; it expires after 1 hour.",
    "created_at": "2026-10-15T09:30:06Z",
    "decision": "answered",
    "source_faq_ids": ["faq_027"],
}


class ConversationMessageResponse(BaseModel):
    """Public view of a message (no session ID or internal sequence number)."""

    model_config = ConfigDict(
        from_attributes=True, json_schema_extra={"examples": [_EXAMPLE_ASSISTANT_MESSAGE]}
    )

    message_id: uuid.UUID
    role: MessageRole
    content: str = Field(description="The message text as shown to the customer.")
    created_at: datetime = Field(description="When the message was recorded (UTC).")
    decision: Decision | None = Field(
        description="For assistant messages: the Stage 4 decision (answered, ambiguous, "
        "no_match or retrieval_unavailable). null for user messages."
    )
    source_faq_ids: list[str] = Field(
        description="For assistant messages: the FAQs the reply is based on (for a "
        "clarification: the FAQs it offers). Empty for user messages and fallbacks."
    )


_EXAMPLE_SESSION = {
    "session_id": _EXAMPLE_SESSION_ID,
    "status": "active",
    "created_at": _EXAMPLE_CREATED,
    "updated_at": "2026-10-15T09:30:06Z",
    "expires_at": _EXAMPLE_EXPIRES,
}
MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = 50


class ConversationCreateResponse(BaseModel):
    """A new conversation and its access token (returned only once, here)."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "session_id": _EXAMPLE_SESSION_ID,
                    "access_token": "cst_<43 random characters>",
                    "token_type": "Bearer",
                    "status": "active",
                    "created_at": _EXAMPLE_CREATED,
                    "expires_at": _EXAMPLE_EXPIRES,
                }
            ]
        },
    )

    session_id: uuid.UUID = Field(
        description="Identifies the conversation. It grants no access on its own."
    )
    access_token: str = Field(
        description="Secret bearer token for this conversation, returned only in this "
        "response and never again. Send it as `Authorization: Bearer <token>` on every "
        "other conversation request. Never put it in a URL. Anyone with it can read, "
        "continue, close and delete the conversation."
    )
    token_type: Literal["Bearer"] = "Bearer"  # noqa: S105 - the scheme name, not a secret
    status: SessionStatus
    created_at: datetime = Field(description="When the conversation was created (UTC).")
    expires_at: datetime | None = Field(
        description="When the conversation stops accepting messages (UTC); null if it "
        "does not expire."
    )

    @classmethod
    def from_session(cls, session: ConversationSession, access_token: str) -> Self:
        return cls(
            session_id=session.session_id,
            access_token=access_token,
            status=session.status,
            created_at=session.created_at,
            expires_at=session.expires_at,
        )


class ConversationSessionResponse(BaseModel):
    """Conversation metadata (no messages, never the token)."""

    model_config = ConfigDict(
        from_attributes=True, json_schema_extra={"examples": [_EXAMPLE_SESSION]}
    )

    session_id: uuid.UUID
    status: SessionStatus = Field(
        description="active: accepts messages. closed: closed by the customer. expired: "
        "past expires_at. Closed and expired conversations stay readable."
    )
    created_at: datetime = Field(description="When the conversation was created (UTC).")
    updated_at: datetime = Field(description="When the conversation last changed (UTC).")
    expires_at: datetime | None = Field(
        description="When the conversation stops accepting messages (UTC)."
    )


class ConversationHistoryResponse(BaseModel):
    """One page of a conversation's messages, oldest first."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "session_id": _EXAMPLE_SESSION_ID,
                    "status": "active",
                    "messages": [_EXAMPLE_USER_MESSAGE, _EXAMPLE_ASSISTANT_MESSAGE],
                    "total_messages": 2,
                    "offset": 0,
                    "limit": DEFAULT_PAGE_SIZE,
                    "has_more": False,
                }
            ]
        }
    )

    session_id: uuid.UUID
    status: SessionStatus
    messages: list[ConversationMessageResponse] = Field(
        description="Messages in the order they were added, oldest first."
    )
    total_messages: int = Field(ge=0, description="Messages in the whole conversation.")
    offset: int = Field(ge=0, description="Messages skipped before this page.")
    limit: int = Field(ge=1, le=MAX_PAGE_SIZE, description="Largest page size requested.")
    has_more: bool = Field(
        description="True when later messages exist: request again with offset + limit."
    )

    @classmethod
    def from_records(
        cls,
        session: ConversationSession,
        messages: Iterable[ConversationMessage],
        *,
        total: int,
        offset: int = 0,
        limit: int = DEFAULT_PAGE_SIZE,
    ) -> Self:
        """Build a page from stored records, in sequence order.

        Raises:
            ValueError: a message belongs to another session, or the page's positions
                are not consecutive from ``offset + 1``.
        """
        ordered = check_message_order(messages, start=offset + 1)
        if ordered and ordered[0].session_id != session.session_id:
            raise ValueError("messages belong to a different session")
        return cls(
            session_id=session.session_id,
            status=session.status,
            messages=[ConversationMessageResponse.model_validate(m) for m in ordered],
            total_messages=total,
            offset=offset,
            limit=limit,
            has_more=offset + len(ordered) < total,
        )


class ConversationChatRequest(BaseModel):
    """A customer message to add to a session.

    Only the text is accepted: the role, IDs and timestamps are set by the server, and
    unknown fields such as ``role`` or ``history`` are rejected.
    """

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"examples": [{"message": "How do I reset my password?"}]},
    )

    message: SearchQueryText = Field(
        description="The customer's message. Repeated and surrounding whitespace is "
        f"removed; must not be blank (1-{MAX_USER_MESSAGE_LENGTH} characters)."
    )
    category: Category | None = Field(
        default=None, description="Only use FAQs in this category (case-insensitive)."
    )


class ConversationChatResponse(BaseModel):
    """The customer's message as recorded and the assistant's reply."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "session_id": _EXAMPLE_SESSION_ID,
                    "user_message": _EXAMPLE_USER_MESSAGE,
                    "assistant_message": _EXAMPLE_ASSISTANT_MESSAGE,
                    "generation_method": "llm_grounded",
                    "usage": {
                        "input_tokens": 850,
                        "output_tokens": 70,
                        "total_tokens": 920,
                        "estimated_cost_usd": None,
                    },
                }
            ]
        }
    )

    session_id: uuid.UUID
    user_message: ConversationMessageResponse
    assistant_message: ConversationMessageResponse
    generation_method: GenerationMethod = Field(
        description="How the reply was produced (see POST /api/v1/answer)."
    )
    usage: UsageSummary | None = Field(
        description="Actual LLM token usage for this reply; null when no LLM call produced "
        "a response."
    )

    @classmethod
    def from_records(
        cls,
        user: ConversationMessage,
        assistant: ConversationMessage,
        generation_method: GenerationMethod,
        usage: UsageSummary | None = None,
    ) -> Self:
        """Build the response from the two stored messages of one exchange.

        Raises:
            ValueError: wrong roles, different sessions, or the reply does not directly
                follow the customer message.
        """
        if user.role is not MessageRole.USER or assistant.role is not MessageRole.ASSISTANT:
            raise ValueError("expected a user message followed by an assistant message")
        if user.session_id != assistant.session_id:
            raise ValueError("messages belong to different sessions")
        if assistant.sequence != user.sequence + 1:
            raise ValueError("the assistant message must directly follow the user message")
        return cls(
            session_id=user.session_id,
            user_message=ConversationMessageResponse.model_validate(user),
            assistant_message=ConversationMessageResponse.model_validate(assistant),
            generation_method=generation_method,
            usage=usage,
        )
