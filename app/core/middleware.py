"""HTTP middleware: request IDs, access logging and a safe error boundary."""

import logging
import time
import uuid

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from app.core.logging_config import request_id_ctx

logger = logging.getLogger("app.request")

REQUEST_ID_HEADER = "X-Request-ID"


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Assign each request an ID, log its outcome, and hide internal errors.

    The ID is generated server-side (never trusted from the client) and returned
    in the X-Request-ID header so a user-reported problem can be matched to logs.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = uuid.uuid4().hex
        token = request_id_ctx.set(request_id)
        start = time.perf_counter()
        try:
            try:
                response = await call_next(request)
            except Exception:
                # Log the full traceback for us, return a generic message to the client.
                logger.exception("Unhandled error on %s %s", request.method, request.url.path)
                response = JSONResponse(
                    status_code=500,
                    content={"detail": "Internal server error", "request_id": request_id},
                )
            duration_ms = (time.perf_counter() - start) * 1000
            logger.info(
                "%s %s -> %d (%.1f ms)",
                request.method,
                request.url.path,
                response.status_code,
                duration_ms,
            )
            response.headers[REQUEST_ID_HEADER] = request_id
            return response
        finally:
            request_id_ctx.reset(token)
