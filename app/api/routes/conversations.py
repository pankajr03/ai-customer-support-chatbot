"""Conversation endpoints (Stage 6): sessions with history, answered from approved FAQs.

Authorization: ``POST /conversations`` returns a session ID and a secret access token
(once). Every other endpoint needs ``Authorization: Bearer <token>`` for that session:

- no ``Authorization`` header -> 401 (checked before any lookup)
- unknown session, malformed ID or wrong token -> 404, identical in every case, so a
  caller cannot tell whether a session exists
- closed or expired conversation (chat only) -> 409

Responses carry ``Cache-Control: no-store``: they contain a secret or private messages.
Tokens are never logged (the access log records the path only, never headers).

Handlers are plain ``def``: FastAPI runs them in a worker thread, so database access,
retrieval and LLM calls never block the event loop.
"""

import math
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Query, Request, Response, status

from app.api.dependencies import (
    AccessTokenDep,
    ConversationChatServiceDep,
    ConversationServiceDep,
)
from app.api.errors import (
    CONVERSATION_NOT_FOUND_DETAIL,
    CONVERSATION_UNAVAILABLE_DETAIL,
    FAQ_UNAVAILABLE_DETAIL,
)
from app.models.conversation import (
    DEFAULT_PAGE_SIZE,
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
AUTHORIZED_ERRORS: dict[int | str, dict[str, Any]] = {
    **UNAUTHORIZED,
    **NOT_FOUND,
    **STORAGE_UNAVAILABLE,
}


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
            "description": "The conversation is closed or expired; start a new one.",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "This conversation is expired and no longer accepts "
                        "messages. Start a new conversation.",
                        "status": "expired",
                        "request_id": "<id>",
                    }
                }
            },
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
) -> ConversationChatResponse:
    """Answer a customer message using approved FAQs and the recent conversation.

    A follow-up such as "What about after 30 days?" is turned into a standalone search
    query using the recent conversation; the answer still comes only from approved FAQs
    through the same retrieval, decision and validation rules as `POST /api/v1/answer`.
    Earlier messages are never used as facts.

    The message and the reply are stored together (both or neither) and returned with
    the decision, the source FAQ IDs and how the answer was produced
    (`generation_method`, as in `POST /api/v1/answer`).
    """
    turn = service.chat(session_id, token, body.message, category=body.category)
    retry_after = turn.decision.retry_after_seconds
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
    """Stop the conversation accepting messages. Its history stays readable.

    Closing a closed or expired conversation changes nothing.
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
    """Permanently delete the conversation and all its messages. The token stops working."""
    service.delete_conversation(session_id, token)
    return Response(status_code=status.HTTP_204_NO_CONTENT, headers={"Cache-Control": "no-store"})
