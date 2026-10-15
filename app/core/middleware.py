"""HTTP middleware: request IDs, access logging and a safe error boundary."""

import logging
import time
import uuid
from typing import Any

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from app.core.logging_config import request_id_ctx, safe_log_value
from app.models.errors import ErrorCode, error_content

logger = logging.getLogger("app.request")

REQUEST_ID_HEADER = "X-Request-ID"
INTERNAL_ERROR_MESSAGE = "Something went wrong on our side. Please try again."


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Assign each request an ID, log its outcome, and hide internal errors.

    The ID is generated server-side (never trusted from the client) and returned
    in the X-Request-ID header so a user-reported problem can be matched to logs.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = uuid.uuid4().hex
        token = request_id_ctx.set(request_id)
        start = time.perf_counter()
        # The path is client-controlled: escape control characters before logging it.
        path = safe_log_value(request.url.path)
        try:
            try:
                response = await call_next(request)
            except Exception:
                # Log the full traceback for us, return a generic message to the client.
                logger.exception("Unhandled error on %s %s", request.method, path)
                logger.error(
                    "event=api_error status=500 code=%s method=%s path=%s",
                    ErrorCode.INTERNAL_ERROR.value,
                    request.method,
                    path,
                )
                response = JSONResponse(
                    status_code=500,
                    content=error_content(ErrorCode.INTERNAL_ERROR, INTERNAL_ERROR_MESSAGE),
                )
            duration_ms = (time.perf_counter() - start) * 1000
            # Path only: query strings, headers and bodies can carry tokens or customer data.
            logger.info(
                "%s %s -> %d (%.1f ms)",
                request.method,
                path,
                response.status_code,
                duration_ms,
            )
            response.headers[REQUEST_ID_HEADER] = request_id
            return response
        finally:
            request_id_ctx.reset(token)


# --- Request size limit ----------------------------------------------------------------------

REQUEST_TOO_LARGE_MESSAGE = "The request body is too large."


class RequestSizeLimitMiddleware:
    """Reject request bodies larger than ``max_bytes`` with 413, before they are parsed.

    - A declared ``Content-Length`` above the limit is rejected without reading the body.
    - A body without one (a chunked upload) is read here first, but never more than
      ``max_bytes + 1`` bytes: if it is too large it is rejected, otherwise it is replayed
      to the application unchanged. At most ``max_bytes`` are ever held in memory.
    """

    def __init__(self, app: Any, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        declared = dict(scope.get("headers") or []).get(b"content-length")
        if declared is not None:
            try:
                too_large = int(declared) > self.max_bytes
            except ValueError:
                too_large = False  # let the server/framework reject a malformed header
            if too_large:
                await too_large_response()(scope, receive, send)
                return
            await self.app(scope, receive, send)
            return

        # No declared length: read (up to the limit) before handing the body on.
        messages: list[Any] = []
        size = 0
        while True:
            message = await receive()
            messages.append(message)
            if message["type"] != "http.request":
                break  # e.g. the client disconnected
            size += len(message.get("body", b""))
            if size > self.max_bytes:
                await too_large_response()(scope, receive, send)
                return
            if not message.get("more_body", False):
                break

        async def replay() -> Any:
            return messages.pop(0) if messages else await receive()

        await self.app(scope, replay, send)


def too_large_response() -> JSONResponse:
    return JSONResponse(
        status_code=413,
        content=error_content(ErrorCode.PAYLOAD_TOO_LARGE, REQUEST_TOO_LARGE_MESSAGE),
    )
