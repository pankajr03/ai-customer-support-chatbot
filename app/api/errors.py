"""Application-wide exception handlers.

Response policy:
- Client mistakes (bad parameters) -> 422 with FastAPI's standard field details.
- Missing resources -> 404, raised directly by routes.
- FAQ data unavailable at request time -> 503 with a generic message.
- Embedding model / semantic index failure -> 503 with a generic message.
- Conversations: every error body is a ``ChatErrorResponse`` (stable ``code``, safe
  ``detail``, ``request_id``). No token -> 401; token in the URL -> 400; unknown
  session or wrong token -> 404 (one message for both, so a caller cannot tell whether
  a session exists); expired -> 410; closed, or another message still being answered
  -> 409 (busy: with Retry-After); an Idempotency-Key or client_message_id reused for a
  different message, or the two disagreeing -> 422; storage failure -> 503 with a
  generic message (with Retry-After when the database was only locked). These
  responses are never cached.
- Anything else -> 500 from RequestContextMiddleware (never a 2xx).

Server-side details (file paths, exception types) go to the logs, never to clients.
Logged validation details name the failing fields and rule types, not the values.
"""

import logging
import math

from fastapi import FastAPI, Request, status
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response

from app.api.dependencies import AccessTokenInURLError, AccessTokenMissingError
from app.core.logging_config import request_id_ctx, safe_log_value
from app.database.connection import DatabaseError
from app.database.faq_repository import FAQDataError
from app.models.chat import ChatErrorCode
from app.repositories.conversation_repository import (
    ConversationStoreBusyError,
    ConversationStoreError,
)
from app.services.conversation_chat_service import (
    ClientMessageIdMismatchError,
    IdempotencyKeyMismatchError,
)
from app.services.conversation_service import (
    ConversationBusyError,
    ConversationExpiredError,
    ConversationNotActiveError,
    ConversationNotFoundError,
)
from app.services.embedding_service import EmbeddingError

logger = logging.getLogger("app.errors")

FAQ_UNAVAILABLE_DETAIL = "The FAQ knowledge base is temporarily unavailable."
SEMANTIC_UNAVAILABLE_DETAIL = "Semantic search is temporarily unavailable."
CONVERSATION_NOT_FOUND_DETAIL = "Conversation not found."
CONVERSATION_UNAVAILABLE_DETAIL = "Conversation storage is temporarily unavailable."
CONVERSATION_EXPIRED_DETAIL = (
    "This conversation has expired and is no longer available. Start a new conversation."
)
CONVERSATION_BUSY_DETAIL = (
    "Another message in this conversation is still being answered. Retry shortly."
)
DUPLICATE_IN_PROGRESS_DETAIL = (
    "This request (same Idempotency-Key) is still being processed. Retry shortly to get its result."
)
IDEMPOTENCY_MISMATCH_DETAIL = (
    "This Idempotency-Key was already used for a different message in this conversation."
)
CLIENT_MESSAGE_ID_MISMATCH_DETAIL = (
    "client_message_id and the Idempotency-Key header must be the same when both are sent."
)
MISSING_AUTHORIZATION_DETAIL = (
    "Missing conversation access token. Send 'Authorization: Bearer <token>'."
)
CREDENTIALS_IN_URL_DETAIL = "Send the access token in the Authorization header, never in the URL."
STORAGE_BUSY_RETRY_SECONDS = 1
NO_STORE = {"Cache-Control": "no-store"}


async def _log_validation_error(request: Request, exc: RequestValidationError) -> Response:
    problems = "; ".join(
        f"{'.'.join(str(part) for part in error['loc'])} ({error['type']})"
        for error in exc.errors()
    )
    logger.info("Rejected %s %s: %s", request.method, safe_log_value(request.url.path), problems)
    return await request_validation_exception_handler(request, exc)


class ChatKnowledgeBaseUnavailableError(Exception):
    """The FAQ knowledge base failed during a chat turn (nothing was stored).

    The chat route raises it ``from`` the original ``FAQDataError``, so the response uses
    the chat error shape; the other FAQ endpoints keep their ``{detail, request_id}``.
    """


def _error(
    status_code: int,
    code: ChatErrorCode,
    detail: str,
    *,
    headers: dict[str, str] | None = None,
    session_status: str | None = None,
) -> JSONResponse:
    """A ``ChatErrorResponse`` body: stable code, safe detail and the request ID."""
    content: dict[str, object] = {
        "code": code.value,
        "detail": detail,
        "request_id": request_id_ctx.get(),
    }
    if session_status is not None:
        content["status"] = session_status
    return JSONResponse(status_code=status_code, content=content, headers=headers)


async def _faq_data_unavailable(request: Request, exc: Exception) -> Response:
    # Expected failure category with a descriptive message: no traceback needed.
    logger.error(
        "FAQ data unavailable on %s %s: %s: %s",
        request.method,
        safe_log_value(request.url.path),
        type(exc).__name__,
        exc,
    )
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"detail": FAQ_UNAVAILABLE_DETAIL, "request_id": request_id_ctx.get()},
    )


async def _chat_knowledge_base_unavailable(request: Request, exc: Exception) -> Response:
    cause = exc.__cause__ or exc
    logger.error(
        "FAQ data unavailable on %s %s: %s: %s",
        request.method,
        safe_log_value(request.url.path),
        type(cause).__name__,
        cause,
    )
    return _error(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        ChatErrorCode.KNOWLEDGE_BASE_UNAVAILABLE,
        FAQ_UNAVAILABLE_DETAIL,
        headers=NO_STORE,
    )


async def _semantic_search_unavailable(request: Request, exc: Exception) -> Response:
    # Model could not load (offline, missing, corrupt) or produced unusable vectors.
    # The model name and cause go to the log; the client gets a generic message.
    logger.error(
        "Semantic search unavailable on %s %s: %s: %s",
        request.method,
        safe_log_value(request.url.path),
        type(exc).__name__,
        exc,
    )
    retry_after = getattr(exc, "retry_after_seconds", None)
    headers = {"Retry-After": str(math.ceil(retry_after))} if retry_after else None
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"detail": SEMANTIC_UNAVAILABLE_DETAIL, "request_id": request_id_ctx.get()},
        headers=headers,
    )


async def _access_token_missing(request: Request, exc: Exception) -> Response:
    return _error(
        status.HTTP_401_UNAUTHORIZED,
        ChatErrorCode.TOKEN_REQUIRED,
        MISSING_AUTHORIZATION_DETAIL,
        headers={**NO_STORE, "WWW-Authenticate": "Bearer"},
    )


async def _access_token_in_url(request: Request, exc: Exception) -> Response:
    return _error(
        status.HTTP_400_BAD_REQUEST,
        ChatErrorCode.TOKEN_IN_URL,
        CREDENTIALS_IN_URL_DETAIL,
        headers=NO_STORE,
    )


async def _conversation_not_found(request: Request, exc: Exception) -> Response:
    # Unknown session, malformed ID and wrong or missing token all end here, identically.
    return _error(
        status.HTTP_404_NOT_FOUND,
        ChatErrorCode.CONVERSATION_NOT_FOUND,
        CONVERSATION_NOT_FOUND_DETAIL,
        headers=NO_STORE,
    )


async def _conversation_not_active(request: Request, exc: Exception) -> Response:
    state = str(getattr(exc, "status", "closed"))
    return _error(
        status.HTTP_409_CONFLICT,
        ChatErrorCode.CONVERSATION_CLOSED,
        f"This conversation is {state} and no longer accepts messages. Start a new conversation.",
        headers=NO_STORE,
        session_status=state,
    )


async def _conversation_expired(request: Request, exc: Exception) -> Response:
    return _error(
        status.HTTP_410_GONE,
        ChatErrorCode.CONVERSATION_EXPIRED,
        CONVERSATION_EXPIRED_DETAIL,
        headers=NO_STORE,
        session_status="expired",
    )


async def _conversation_busy(request: Request, exc: Exception) -> Response:
    duplicate = getattr(exc, "duplicate", False)
    retry_after = math.ceil(getattr(exc, "retry_after_seconds", 1))
    return _error(
        status.HTTP_409_CONFLICT,
        ChatErrorCode.DUPLICATE_IN_PROGRESS if duplicate else ChatErrorCode.CONVERSATION_BUSY,
        DUPLICATE_IN_PROGRESS_DETAIL if duplicate else CONVERSATION_BUSY_DETAIL,
        headers={**NO_STORE, "Retry-After": str(retry_after)},
    )


async def _idempotency_key_mismatch(request: Request, exc: Exception) -> Response:
    detail = (
        CLIENT_MESSAGE_ID_MISMATCH_DETAIL
        if isinstance(exc, ClientMessageIdMismatchError)
        else IDEMPOTENCY_MISMATCH_DETAIL
    )
    return _error(
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        ChatErrorCode.IDEMPOTENCY_KEY_MISMATCH,
        detail,
        headers=NO_STORE,
    )


async def _conversation_storage_unavailable(request: Request, exc: Exception) -> Response:
    # The exception type only: database messages can contain SQL parameters (customer text).
    logger.error(
        "Conversation storage unavailable on %s %s: %s",
        request.method,
        safe_log_value(request.url.path),
        type(exc).__name__,
    )
    headers = dict(NO_STORE)
    if isinstance(exc, ConversationStoreBusyError):  # locked by another writer: retry soon
        headers["Retry-After"] = str(STORAGE_BUSY_RETRY_SECONDS)
    return _error(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        ChatErrorCode.CONVERSATION_STORAGE_UNAVAILABLE,
        CONVERSATION_UNAVAILABLE_DETAIL,
        headers=headers,
    )


def register_exception_handlers(app: FastAPI) -> None:
    """Attach the handlers above to an application."""
    app.add_exception_handler(RequestValidationError, _log_validation_error)
    app.add_exception_handler(FAQDataError, _faq_data_unavailable)
    app.add_exception_handler(ChatKnowledgeBaseUnavailableError, _chat_knowledge_base_unavailable)
    app.add_exception_handler(EmbeddingError, _semantic_search_unavailable)
    app.add_exception_handler(AccessTokenMissingError, _access_token_missing)
    app.add_exception_handler(AccessTokenInURLError, _access_token_in_url)
    app.add_exception_handler(ClientMessageIdMismatchError, _idempotency_key_mismatch)
    app.add_exception_handler(ConversationNotFoundError, _conversation_not_found)
    app.add_exception_handler(ConversationNotActiveError, _conversation_not_active)
    app.add_exception_handler(ConversationExpiredError, _conversation_expired)
    app.add_exception_handler(ConversationBusyError, _conversation_busy)
    app.add_exception_handler(IdempotencyKeyMismatchError, _idempotency_key_mismatch)
    app.add_exception_handler(ConversationStoreError, _conversation_storage_unavailable)
    app.add_exception_handler(DatabaseError, _conversation_storage_unavailable)
