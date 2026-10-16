"""Conversation endpoints (Stage 6): sessions with history, answered from approved FAQs.

Authorization: ``POST /conversations`` returns a session ID and a secret access token
(once). Every other endpoint needs ``Authorization: Bearer <token>`` for that session:

- no ``Authorization`` header -> 401 ``UNAUTHORIZED_SESSION`` (checked before any lookup)
- unknown session, malformed ID or wrong token -> 404 ``SESSION_NOT_FOUND``, identical
  in every case, so a caller cannot tell whether a session exists
- expired conversation -> 410 ``SESSION_EXPIRED`` (only DELETE still works)
- chat on a closed conversation -> 409 ``SESSION_CLOSED``; while another message to it
  is being answered -> 409 ``REQUEST_IN_PROGRESS`` with Retry-After

The chat handler is deliberately thin: validation, idempotency, retrieval, generation and
storage are all in ``CustomerChatService`` (``app.services.customer_chat_service``).

``/chat`` follows the customer chat contract (``app.models.chat``): the decision is one
of answered / no_match / ambiguous / retrieval_unavailable, and every error body is an
``ErrorResponse`` (``app.models.errors``). A ``client_message_id`` in the body (or
the equivalent ``Idempotency-Key`` header) makes retries safe: repeating a request with
the same ID returns the stored result (``Idempotent-Replayed: true``) instead of
answering and storing the message twice.

Responses carry ``Cache-Control: no-store``: they contain a secret or private messages.
Tokens are never logged (the access log records the path only, never headers).

Handlers are plain ``def``: FastAPI runs them in a worker thread, so database access,
retrieval and LLM calls never block the event loop.
"""

import math
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Path, Query, Request, Response, status

from app.api.dependencies import (
    AccessTokenDep,
    ConversationServiceDep,
    CustomerChatServiceDep,
    FAQServiceDep,
    limit_chat_messages,
    limit_new_conversations,
)
from app.api.errors import (
    BUSY_MESSAGE,
    CHANGED_MESSAGE,
    DUPLICATE_IN_PROGRESS_MESSAGE,
    DUPLICATE_MESSAGE_MESSAGE,
    FAQ_UNAVAILABLE_MESSAGE,
    MISSING_AUTHORIZATION_MESSAGE,
    SESSION_CLOSED_MESSAGE,
    SESSION_EXPIRED_MESSAGE,
    SESSION_NOT_FOUND_MESSAGE,
    STORAGE_UNAVAILABLE_MESSAGE,
    VALIDATION_MESSAGE,
)
from app.core.middleware import RATE_LIMITED_MESSAGE
from app.models.chat import CustomerChatRequest, CustomerChatResponse
from app.models.conversation import (
    DEFAULT_PAGE_SIZE,
    IDEMPOTENCY_KEY_PATTERN,
    MAX_PAGE_SIZE,
    ConversationCreateResponse,
    ConversationHistoryResponse,
    ConversationSessionResponse,
)
from app.models.errors import ErrorCode, ErrorResponse, current_request_id, error_example


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


router = APIRouter(
    prefix="/conversations", tags=["conversations"], dependencies=[Depends(_no_store)]
)

SessionId = Annotated[
    str,
    Path(
        max_length=64,
        description="The `session_id` returned when the conversation was created.",
        examples=["3f2b8c1e-7a4d-4e9b-9c61-2d5f0a8e4b17"],
    ),
]


def _error(description: str, *examples: tuple[str, ErrorCode, str]) -> dict[str, Any]:
    """An OpenAPI error response with one or more named examples."""
    values = {name: {"value": error_example(code, message)} for name, code, message in examples}
    return {
        "model": ErrorResponse,
        "description": description,
        "content": {"application/json": {"examples": values}},
    }


UNAUTHORIZED = {
    status.HTTP_401_UNAUTHORIZED: _error(
        "No `Authorization: Bearer <token>` header.",
        ("missing_token", ErrorCode.UNAUTHORIZED_SESSION, MISSING_AUTHORIZATION_MESSAGE),
    )
}
NOT_FOUND = {
    status.HTTP_404_NOT_FOUND: _error(
        "Unknown conversation, or the token does not grant access to it (indistinguishable "
        "on purpose).",
        ("not_found", ErrorCode.SESSION_NOT_FOUND, SESSION_NOT_FOUND_MESSAGE),
    )
}
STORAGE_UNAVAILABLE = {
    status.HTTP_503_SERVICE_UNAVAILABLE: _error(
        "Conversation storage is temporarily unavailable.",
        ("storage", ErrorCode.SERVICE_UNAVAILABLE, STORAGE_UNAVAILABLE_MESSAGE),
    )
}
EXPIRED = {
    status.HTTP_410_GONE: _error(
        "The conversation has expired. Only DELETE still works; start a new conversation.",
        ("expired", ErrorCode.SESSION_EXPIRED, SESSION_EXPIRED_MESSAGE),
    )
}
VALIDATION = {
    status.HTTP_422_UNPROCESSABLE_CONTENT: _error(
        "Invalid parameters. `error.details` lists the fields (without their values).",
        ("validation", ErrorCode.VALIDATION_ERROR, VALIDATION_MESSAGE),
    )
}
RATE_LIMITED = {
    status.HTTP_429_TOO_MANY_REQUESTS: _error(
        "Too many requests (process-local limits: per client IP, and per conversation for "
        "chat messages). Retry after `Retry-After` seconds.",
        ("rate_limited", ErrorCode.RATE_LIMITED, RATE_LIMITED_MESSAGE),
    )
}
AUTHORIZED_ERRORS: dict[int | str, dict[str, Any]] = {
    **UNAUTHORIZED,
    **NOT_FOUND,
    **EXPIRED,
    **VALIDATION,
    **STORAGE_UNAVAILABLE,
}

IdempotencyKeyHeader = Annotated[
    str | None,
    Header(
        alias="Idempotency-Key",
        pattern=IDEMPOTENCY_KEY_PATTERN,
        description="Optional client-chosen key (e.g. a UUID, 1-64 of A-Z a-z 0-9 . _ : -) "
        "for safe retries: repeating the request with the same key returns the stored "
        "result instead of answering again. Reusing it for a different message is a 422. "
        "Prefer `client_message_id` in the body; if both are sent they must name the same "
        "UUID.",
    ),
]


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=ConversationCreateResponse,
    summary="Start a conversation",
    responses={**STORAGE_UNAVAILABLE, **RATE_LIMITED},
    dependencies=[Depends(limit_new_conversations)],
)
def create_conversation(
    service: ConversationServiceDep, request: Request, response: Response
) -> ConversationCreateResponse:
    """Create a conversation and return its ID and **secret access token**.

    The token is returned only in this response and cannot be recovered. Keep it like a
    password: send it as `Authorization: Bearer <token>` on every other conversation
    request, never in a URL. No authorization is needed to create a conversation.
    """
    created = service.create_conversation()
    response.headers["Location"] = str(
        request.url_for("get_conversation", session_id=str(created.session.session_id))
    )
    return ConversationCreateResponse.from_session(created.session, created.access_token)


@router.get(
    "/{session_id}",
    response_model=ConversationSessionResponse,
    summary="Get conversation details",
    responses=AUTHORIZED_ERRORS,
)
def get_conversation(
    session_id: SessionId, token: AccessTokenDep, service: ConversationServiceDep
) -> ConversationSessionResponse:
    """Status and timestamps of the conversation (no messages)."""
    return ConversationSessionResponse.model_validate(service.get_conversation(session_id, token))


@router.get(
    "/{session_id}/messages",
    response_model=ConversationHistoryResponse,
    summary="Get conversation messages",
    responses=AUTHORIZED_ERRORS,
)
def get_messages(
    session_id: SessionId,
    token: AccessTokenDep,
    service: ConversationServiceDep,
    faqs: FAQServiceDep,
    offset: Annotated[
        int, Query(ge=0, le=1_000_000, description="Messages to skip, oldest first.")
    ] = 0,
    limit: Annotated[
        int, Query(ge=1, le=MAX_PAGE_SIZE, description=f"Page size (1-{MAX_PAGE_SIZE}).")
    ] = DEFAULT_PAGE_SIZE,
) -> ConversationHistoryResponse:
    """One page of the conversation's messages, oldest first.

    For the next page, repeat the request with `offset + limit` while `has_more` is true.
    Closed conversations stay readable; expired ones give `410` (only DELETE still works).

    Each reply lists its `sources` (FAQ ID and approved question) like the chat response,
    so a client that lost a reply can show it again with its references.
    """
    session, messages, total = service.get_messages_page(
        session_id, token, offset=offset, limit=limit
    )
    questions = {
        faq_id: faq.question
        for faq_id in {faq_id for m in messages for faq_id in m.source_faq_ids}
        if (faq := faqs.get_active_faq(faq_id)) is not None
    }
    return ConversationHistoryResponse.from_records(
        session, messages, total=total, offset=offset, limit=limit, questions=questions
    )


@router.post(
    "/{session_id}/chat",
    response_model=CustomerChatResponse,
    summary="Send a message and get an answer",
    dependencies=[Depends(limit_chat_messages)],
    responses={
        **AUTHORIZED_ERRORS,
        **RATE_LIMITED,
        status.HTTP_409_CONFLICT: _error(
            "The conversation is closed, or another message to it is being answered (with "
            "Retry-After), or was answered while this one was (send it again).",
            ("closed", ErrorCode.SESSION_CLOSED, SESSION_CLOSED_MESSAGE),
            ("busy", ErrorCode.REQUEST_IN_PROGRESS, BUSY_MESSAGE),
            (
                "same_message_in_progress",
                ErrorCode.REQUEST_IN_PROGRESS,
                DUPLICATE_IN_PROGRESS_MESSAGE,
            ),
            ("conversation_changed", ErrorCode.REQUEST_IN_PROGRESS, CHANGED_MESSAGE),
        ),
        status.HTTP_422_UNPROCESSABLE_CONTENT: _error(
            "Invalid request: not a JSON object, unknown fields, a blank message, control "
            "characters, a message longer than MAX_CUSTOMER_MESSAGE_CHARACTERS, or a "
            "malformed client_message_id / Idempotency-Key (`VALIDATION_ERROR`, with "
            "`error.details`). Or a client_message_id reused for a different request "
            "(`DUPLICATE_MESSAGE_CONFLICT`). Nothing is stored.",
            ("validation", ErrorCode.VALIDATION_ERROR, VALIDATION_MESSAGE),
            ("duplicate_message", ErrorCode.DUPLICATE_MESSAGE_CONFLICT, DUPLICATE_MESSAGE_MESSAGE),
        ),
        status.HTTP_503_SERVICE_UNAVAILABLE: _error(
            "Conversation storage or the FAQ knowledge base is temporarily unavailable. "
            "Nothing was stored.",
            ("storage", ErrorCode.SERVICE_UNAVAILABLE, STORAGE_UNAVAILABLE_MESSAGE),
            ("knowledge_base", ErrorCode.SERVICE_UNAVAILABLE, FAQ_UNAVAILABLE_MESSAGE),
        ),
    },
)
def chat(
    session_id: SessionId,
    body: CustomerChatRequest,
    token: AccessTokenDep,
    service: CustomerChatServiceDep,
    response: Response,
    idempotency_key: IdempotencyKeyHeader = None,
) -> CustomerChatResponse:
    """Answer a customer message using approved FAQs and the recent conversation.

    The body must be a JSON object (`Content-Type: application/json`). The message may
    contain line breaks and any language or emoji; only whitespace around it is removed.
    At most `MAX_CUSTOMER_MESSAGE_CHARACTERS` characters (default 4000).

    A follow-up such as "What about after 30 days?" is turned into a standalone search
    query using the recent conversation; the answer still comes only from approved FAQs
    through the same retrieval, decision and validation rules as `POST /api/v1/answer`.
    Earlier messages are never used as facts.

    The message and the reply are stored together (both or neither). The reply has a
    `decision`:

    - `answered`: an approved or grounded `answer` with its `sources`;
    - `no_match`: a safe fallback message (no sources);
    - `ambiguous`: a clarifying question (`sources` are the FAQs it offers);
    - `retrieval_unavailable`: search is temporarily unavailable; try again later (no
      sources; with `Retry-After` when known).

    One message per conversation is answered at a time: while one is in progress, another
    gets `409` with `Retry-After`. Send a `client_message_id` (a UUID) to retry safely
    after a timeout or network error; a replayed result has `Idempotent-Replayed: true`
    and `usage: null` (no new LLM call was made).

    `user_message` and `assistant_message` are the deprecated Stage 6 shape of the same
    exchange, kept for compatibility.
    """
    turn = service.chat(
        session_id, token, body, idempotency_key=idempotency_key, request_id=current_request_id()
    )
    if turn.replayed:
        response.headers["Idempotent-Replayed"] = "true"
    if turn.retry_after_seconds:
        response.headers["Retry-After"] = str(math.ceil(turn.retry_after_seconds))
    return turn.response


@router.post(
    "/{session_id}/close",
    response_model=ConversationSessionResponse,
    summary="Close a conversation",
    responses=AUTHORIZED_ERRORS,
)
def close_conversation(
    session_id: SessionId, token: AccessTokenDep, service: ConversationServiceDep
) -> ConversationSessionResponse:
    """Stop the conversation accepting messages. Its history stays readable until the
    conversation expires.

    Closing a closed conversation changes nothing; an expired one gives `410`.
    """
    return ConversationSessionResponse.model_validate(service.close_conversation(session_id, token))


@router.delete(
    "/{session_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a conversation",
    responses=AUTHORIZED_ERRORS,
)
def delete_conversation(
    session_id: SessionId, token: AccessTokenDep, service: ConversationServiceDep
) -> Response:
    """Permanently delete the conversation and all its messages. The token stops working.

    Also works after the conversation has expired.
    """
    service.delete_conversation(session_id, token)
    return Response(status_code=status.HTTP_204_NO_CONTENT, headers={"Cache-Control": "no-store"})
