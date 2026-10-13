"""Application-wide exception handlers.

Response policy:
- Client mistakes (bad parameters) -> 422 with FastAPI's standard field details.
- Missing resources -> 404, raised directly by routes.
- FAQ data unavailable at request time -> 503 with a generic message.
- Embedding model / semantic index failure -> 503 with a generic message.
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
from app.database.faq_repository import FAQDataError
from app.services.embedding_service import EmbeddingError

logger = logging.getLogger("app.errors")

FAQ_UNAVAILABLE_DETAIL = "The FAQ knowledge base is temporarily unavailable."
SEMANTIC_UNAVAILABLE_DETAIL = "Semantic search is temporarily unavailable."


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


def register_exception_handlers(app: FastAPI) -> None:
    """Attach the handlers above to an application."""
    app.add_exception_handler(RequestValidationError, _log_validation_error)
    app.add_exception_handler(FAQDataError, _faq_data_unavailable)
    app.add_exception_handler(EmbeddingError, _semantic_search_unavailable)
