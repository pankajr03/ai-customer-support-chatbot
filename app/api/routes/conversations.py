"""Conversation endpoints (Stage 6): sessions with history, answered from approved FAQs.

Authorization: ``POST /conversations`` returns a session ID and a secret access token
(once). Every other endpoint needs ``Authorization: Bearer <token>`` for that session:

- no ``Authorization`` header -> 401 (checked before any lookup)
- unknown session, malformed ID or wrong token -> 404, identical in every case, so a
  caller cannot tell whether a session exists
- expired conversation -> 410 (only DELETE still works)
- chat on a closed conversation, or while another message to it is being answered
  -> 409 (busy: with Retry-After)

``/chat`` follows the customer chat contract (``app.models.chat``): the decision is one
of answered / no_match / ambiguous / retrieval_unavailable, and every error body is a
``ChatErrorResponse`` with a stable ``code``. A ``client_message_id`` in the body (or
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
    ConversationChatServiceDep,
    ConversationServiceDep,
)
from app.api.errors import (
    CONVERSATION_BUSY_DETAIL,
    CONVERSATION_EXPIRED_DETAIL,
    CONVERSATION_NOT_FOUND_DETAIL,
    CONVERSATION_UNAVAILABLE_DETAIL,
    FAQ_UNAVAILABLE_DETAIL,
    MISSING_AUTHORIZATION_DETAIL,
    ChatKnowledgeBaseUnavailableError,
)
from app.core.logging_config import request_id_ctx
from app.database.faq_repository import FAQDataError
from app.models.chat import (
    ChatErrorCode,
    ChatErrorResponse,
    CustomerChatRequest,
    CustomerChatResponse,
)
from app.models.conversation import (
    DEFAULT_PAGE_SIZE,
    IDEMPOTENCY_KEY_PATTERN,
    MAX_PAGE_SIZE,
    ConversationCreateResponse,
    ConversationHistoryResponse,
    ConversationSessionResponse,
)
from app.services.conversation_chat_service import resolve_idempotency_key


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


def _body(code: ChatErrorCode, detail: str, **extra: str) -> dict[str, Any]:
    return {"code": code.value, "detail": detail, "request_id": "<id>", **extra}


def _error(description: str, code: ChatErrorCode, detail: str, **extra: str) -> dict[str, Any]:
    return {
        "model": ChatErrorResponse,
        "description": description,
        "content": {"application/json": {"example": _body(code, detail, **extra)}},
    }


UNAUTHORIZED = {
    status.HTTP_401_UNAUTHORIZED: _error(
        "No `Authorization: Bearer <token>` header.",
        ChatErrorCode.TOKEN_REQUIRED,
        MISSING_AUTHORIZATION_DETAIL,
    )
}
NOT_FOUND = {
    status.HTTP_404_NOT_FOUND: _error(
        "Unknown conversation, or the token does not grant access to it (indistinguishable "
        "on purpose).",
        ChatErrorCode.CONVERSATION_NOT_FOUND,
        CONVERSATION_NOT_FOUND_DETAIL,
    )
}
STORAGE_UNAVAILABLE = {
    status.HTTP_503_SERVICE_UNAVAILABLE: _error(
        "Conversation storage is temporarily unavailable.",
        ChatErrorCode.CONVERSATION_STORAGE_UNAVAILABLE,
        CONVERSATION_UNAVAILABLE_DETAIL,
    )
}
EXPIRED = {
    status.HTTP_410_GONE: _error(
        "The conversation has expired. Only DELETE still works; start a new conversation.",
        ChatErrorCode.CONVERSATION_EXPIRED,
        CONVERSATION_EXPIRED_DETAIL,
        status="expired",
    )
}
AUTHORIZED_ERRORS: dict[int | str, dict[str, Any]] = {
    **UNAUTHORIZED,
    **NOT_FOUND,
    **EXPIRED,
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
    responses={**STORAGE_UNAVAILABLE},
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
    offset: Annotated[
        int, Query(ge=0, le=1_000_000, description="Messages to skip, oldest first.")
    ] = 0,
    limit: Annotated[
        int, Query(ge=1, le=MAX_PAGE_SIZE, description=f"Page size (1-{MAX_PAGE_SIZE}).")
    ] = DEFAULT_PAGE_SIZE,
) -> ConversationHistoryResponse:
    """One page of the conversation's messages, oldest first.

    For the next page, repeat the request with `offset + limit` while `has_more` is true.
    Closed and expired conversations stay readable.
    """
    session, messages, total = service.get_messages_page(
        session_id, token, offset=offset, limit=limit
    )
    return ConversationHistoryResponse.from_records(
        session, messages, total=total, offset=offset, limit=limit
    )


@router.post(
    "/{session_id}/chat",
    response_model=CustomerChatResponse,
    summary="Send a message and get an answer",
    responses={
        **AUTHORIZED_ERRORS,
        status.HTTP_409_CONFLICT: {
            "description": "The conversation is closed, or another message to it is still "
            "being answered (then with a Retry-After header).",
            "model": ChatErrorResponse,
            "content": {
                "application/json": {
                    "examples": {
                        "busy": {
                            "value": _body(
                                ChatErrorCode.CONVERSATION_BUSY, CONVERSATION_BUSY_DETAIL
                            )
                        },
                        "closed": {
                            "value": _body(
                                ChatErrorCode.CONVERSATION_CLOSED,
                                "This conversation is closed and no longer accepts "
                                "messages. Start a new conversation.",
                                status="closed",
                            )
                        },
                    }
                }
            },
        },
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": "Invalid body or Idempotency-Key (FastAPI's standard `detail` "
            "list), or a client_message_id / Idempotency-Key reused for a different message "
            "or disagreeing with each other (`code: idempotency_key_mismatch`)."
        },
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "description": "Conversation storage or the FAQ knowledge base is temporarily "
            "unavailable. Nothing was stored.",
            "model": ChatErrorResponse,
            "content": {
                "application/json": {
                    "examples": {
                        "storage": {
                            "value": _body(
                                ChatErrorCode.CONVERSATION_STORAGE_UNAVAILABLE,
                                CONVERSATION_UNAVAILABLE_DETAIL,
                            )
                        },
                        "knowledge_base": {
                            "value": _body(
                                ChatErrorCode.KNOWLEDGE_BASE_UNAVAILABLE, FAQ_UNAVAILABLE_DETAIL
                            )
                        },
                    }
                }
            },
        },
    },
)
def chat(
    session_id: SessionId,
    body: CustomerChatRequest,
    token: AccessTokenDep,
    service: ConversationChatServiceDep,
    response: Response,
    idempotency_key: IdempotencyKeyHeader = None,
) -> CustomerChatResponse:
    """Answer a customer message using approved FAQs and the recent conversation.

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
    key = resolve_idempotency_key(body.client_message_id, idempotency_key)
    try:
        turn = service.chat(
            session_id, token, body.message, category=body.category, idempotency_key=key
        )
    except FAQDataError as exc:
        raise ChatKnowledgeBaseUnavailableError() from exc
    if turn.replayed:
        response.headers["Idempotent-Replayed"] = "true"
    retry_after = turn.decision.retry_after_seconds if turn.decision else None
    if retry_after:
        response.headers["Retry-After"] = str(math.ceil(retry_after))
    request_id = request_id_ctx.get()
    return turn.response.model_copy(
        update={"request_id": request_id if request_id != "-" else None}
    )


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
