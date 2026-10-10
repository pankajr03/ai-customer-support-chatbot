"""Application-wide exception handlers.

Response policy:
- Client mistakes (bad parameters) -> 422 with FastAPI's standard field details.
- Missing resources -> 404, raised directly by routes.
- FAQ data unavailable at request time -> 503 with a generic message.
- Anything else -> 500 from RequestContextMiddleware (never a 2xx).

Server-side details (file paths, exception types) go to the logs, never to clients.
Logged validation details name the failing fields and rule types, not the values.
"""

import logging

from fastapi import FastAPI, Request, status
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response

from app.core.logging_config import request_id_ctx, safe_log_value
from app.database.faq_repository import FAQDataError

logger = logging.getLogger("app.errors")

FAQ_UNAVAILABLE_DETAIL = "The FAQ knowledge base is temporarily unavailable."


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


def register_exception_handlers(app: FastAPI) -> None:
    """Attach the handlers above to an application."""
    app.add_exception_handler(RequestValidationError, _log_validation_error)
    app.add_exception_handler(FAQDataError, _faq_data_unavailable)
