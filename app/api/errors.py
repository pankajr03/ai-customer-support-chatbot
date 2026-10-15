"""Application-wide exception handlers.

Response policy:
- Client mistakes (bad parameters) -> 422 with FastAPI's standard field details.
- Missing resources -> 404, raised directly by routes.
- FAQ data unavailable at request time -> 503 with a generic message.
- Embedding model / semantic index failure -> 503 with a generic message.
- Conversations: unknown session or wrong token -> 404 (one message for both, so a
  caller cannot tell whether a session exists); closed or expired -> 409; storage
  failure -> 503 with a generic message. These responses are never cached.
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

from app.core.logging_config import request_id_ctx, safe_log_value
from app.database.connection import DatabaseError
from app.database.faq_repository import FAQDataError
from app.repositories.conversation_repository import ConversationStoreError
from app.services.conversation_service import (
    ConversationNotActiveError,
    ConversationNotFoundError,
)
from app.services.embedding_service import EmbeddingError

logger = logging.getLogger("app.errors")

FAQ_UNAVAILABLE_DETAIL = "The FAQ knowledge base is temporarily unavailable."
SEMANTIC_UNAVAILABLE_DETAIL = "Semantic search is temporarily unavailable."
CONVERSATION_NOT_FOUND_DETAIL = "Conversation not found."
CONVERSATION_UNAVAILABLE_DETAIL = "Conversation storage is temporarily unavailable."
NO_STORE = {"Cache-Control": "no-store"}


async def _log_validation_error(request: Request, exc: RequestValidationError) -> Response:
    problems = "; ".join(
        f"{'.'.join(str(part) for part in error['loc'])} ({error['type']})"
        for error in exc.errors()
    )
    logger.info("Rejected %s %s: %s", request.method, safe_log_value(request.url.path), problems)
    return await request_validation_exception_handler(request, exc)


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


async def _conversation_not_found(request: Request, exc: Exception) -> Response:
    # Unknown session, malformed ID and wrong or missing token all end here, identically.
    return JSONResponse(
        status_code=status.HTTP_404_NOT_FOUND,
        content={"detail": CONVERSATION_NOT_FOUND_DETAIL, "request_id": request_id_ctx.get()},
        headers=NO_STORE,
    )


async def _conversation_not_active(request: Request, exc: Exception) -> Response:
    state = getattr(exc, "status", "closed")
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content={
            "detail": f"This conversation is {state} and no longer accepts messages. "
            "Start a new conversation.",
            "status": str(state),
            "request_id": request_id_ctx.get(),
        },
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
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"detail": CONVERSATION_UNAVAILABLE_DETAIL, "request_id": request_id_ctx.get()},
        headers=NO_STORE,
    )


def register_exception_handlers(app: FastAPI) -> None:
    """Attach the handlers above to an application."""
    app.add_exception_handler(RequestValidationError, _log_validation_error)
    app.add_exception_handler(FAQDataError, _faq_data_unavailable)
    app.add_exception_handler(EmbeddingError, _semantic_search_unavailable)
    app.add_exception_handler(ConversationNotFoundError, _conversation_not_found)
    app.add_exception_handler(ConversationNotActiveError, _conversation_not_active)
    app.add_exception_handler(ConversationStoreError, _conversation_storage_unavailable)
    app.add_exception_handler(DatabaseError, _conversation_storage_unavailable)
