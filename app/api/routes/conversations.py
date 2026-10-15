"""Conversation endpoints (Stage 6): sessions with history, answered from approved FAQs.

Authorization: ``POST /conversations`` returns a session ID and a secret access token
(once). Every other endpoint needs ``Authorization: Bearer <token>`` for that session:

- no ``Authorization`` header -> 401 (checked before any lookup)
- unknown session, malformed ID or wrong token -> 404, identical in every case, so a
  caller cannot tell whether a session exists
- expired conversation -> 410 (only DELETE still works)
- chat on a closed conversation, or while another message to it is being answered
  -> 409 (busy: with Retry-After)

``/chat`` accepts an optional ``Idempotency-Key`` header: repeating a request with the
same key returns the stored result (``Idempotent-Replayed: true``) instead of answering
and storing the message twice.

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
)
from app.models.conversation import (
    DEFAULT_PAGE_SIZE,
    IDEMPOTENCY_KEY_PATTERN,
    MAX_PAGE_SIZE,
    ConversationChatRequest,
    ConversationChatResponse,
    ConversationCreateResponse,
    ConversationHistoryResponse,
    ConversationSessionResponse,
)


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


def _error(description: str, detail: str) -> dict[str, Any]:
    return {
        "description": description,
        "content": {"application/json": {"example": {"detail": detail, "request_id": "<id>"}}},
    }


UNAUTHORIZED = {
    status.HTTP_401_UNAUTHORIZED: {
        "description": "No `Authorization: Bearer <token>` header.",
        "content": {
            "application/json": {
                "example": {
                    "detail": "Missing conversation access token. Send "
                    "'Authorization: Bearer <token>'."
                }
            }
        },
    }
}
NOT_FOUND = {
    status.HTTP_404_NOT_FOUND: _error(
        "Unknown conversation, or the token does not grant access to it (indistinguishable "
        "on purpose).",
        CONVERSATION_NOT_FOUND_DETAIL,
    )
}
STORAGE_UNAVAILABLE = {
    status.HTTP_503_SERVICE_UNAVAILABLE: _error(
        "Conversation storage is temporarily unavailable.", CONVERSATION_UNAVAILABLE_DETAIL
    )
}
EXPIRED = {
    status.HTTP_410_GONE: _error(
        "The conversation has expired. Only DELETE still works; start a new conversation.",
        CONVERSATION_EXPIRED_DETAIL,
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
        "result instead of answering again. Reusing it for a different message is a 422.",
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
    response_model=ConversationChatResponse,
    summary="Send a message and get an answer",
    responses={
        **AUTHORIZED_ERRORS,
        status.HTTP_409_CONFLICT: {
            "description": "The conversation is closed, or another message to it is still "
            "being answered (then with a Retry-After header).",
            "content": {
                "application/json": {
                    "examples": {
                        "busy": {
                            "value": {"detail": CONVERSATION_BUSY_DETAIL, "request_id": "<id>"}
                        },
                        "closed": {
                            "value": {
                                "detail": "This conversation is closed and no longer accepts "
                                "messages. Start a new conversation.",
                                "status": "closed",
                                "request_id": "<id>",
                            }
                        },
                    }
                }
            },
        },
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": "Invalid body or Idempotency-Key, or a key reused for a different "
            "message."
        },
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "description": "Conversation storage or the FAQ knowledge base is temporarily "
            "unavailable. Nothing was stored.",
            "content": {
                "application/json": {
                    "examples": {
                        "storage": {
                            "value": {
                                "detail": CONVERSATION_UNAVAILABLE_DETAIL,
                                "request_id": "<id>",
                            }
                        },
                        "knowledge_base": {
                            "value": {"detail": FAQ_UNAVAILABLE_DETAIL, "request_id": "<id>"}
                        },
                    }
                }
            },
        },
    },
)
def chat(
    session_id: SessionId,
    body: ConversationChatRequest,
    token: AccessTokenDep,
    service: ConversationChatServiceDep,
    response: Response,
    idempotency_key: IdempotencyKeyHeader = None,
) -> ConversationChatResponse:
    """Answer a customer message using approved FAQs and the recent conversation.

    A follow-up such as "What about after 30 days?" is turned into a standalone search
    query using the recent conversation; the answer still comes only from approved FAQs
    through the same retrieval, decision and validation rules as `POST /api/v1/answer`.
    Earlier messages are never used as facts.

    The message and the reply are stored together (both or neither) and returned with
    the decision, the source FAQ IDs and how the answer was produced
    (`generation_method`, as in `POST /api/v1/answer`).

    One message per conversation is answered at a time: while one is in progress, another
    gets `409` with `Retry-After`. Send an `Idempotency-Key` to retry safely after a
    timeout or network error; a replayed result has `Idempotent-Replayed: true` and
    `usage: null` (no new LLM call was made).
    """
    turn = service.chat(
        session_id, token, body.message, category=body.category, idempotency_key=idempotency_key
    )
    if turn.replayed:
        response.headers["Idempotent-Replayed"] = "true"
    retry_after = turn.decision.retry_after_seconds if turn.decision else None
    if retry_after:
        response.headers["Retry-After"] = str(math.ceil(retry_after))
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
