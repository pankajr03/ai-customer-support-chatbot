"""HTTP middleware: request IDs, access logging and a safe error boundary."""

import logging
import time
import uuid
from typing import Any

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from app.core.logging_config import request_id_ctx, safe_log_value
from app.core.rate_limit import RateLimiter, RateLimitExceededError, retry_after_header
from app.models.errors import ErrorCode, error_content

logger = logging.getLogger("app.request")

REQUEST_ID_HEADER = "X-Request-ID"
INTERNAL_ERROR_MESSAGE = "Something went wrong on our side. Please try again."

# Response hardening that does not depend on the hosting (Stage 12). The API serves JSON
# only: nothing may sniff it into another type, frame it, send a Referer with it, or load
# anything from it. HSTS needs HTTPS and belongs to the TLS-terminating proxy.
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
}
# Interactive docs (never in production) load Swagger UI / ReDoc scripts from a CDN, so
# they get every header except the strict Content-Security-Policy.
DOCS_PATHS = ("/docs", "/redoc")


def add_security_headers(response: Response, path: str) -> None:
    for name, value in SECURITY_HEADERS.items():
        if name == "Content-Security-Policy" and path.startswith(DOCS_PATHS):
            continue
        response.headers.setdefault(name, value)


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
            add_security_headers(response, request.url.path)
            return response
        finally:
            request_id_ctx.reset(token)


# --- Request rate limit ----------------------------------------------------------------------

RATE_LIMITED_MESSAGE = "Too many requests. Please wait a moment and try again."


class RequestRateLimitMiddleware:
    """``429 RATE_LIMITED`` once a client IP exceeds its per-minute request allowance.

    Only paths under ``prefix`` count, except ``exempt`` (health checks, which load
    balancers call often). Runs inside ``RequestContextMiddleware``, so a refused request
    still gets a request ID and an access-log line. Process-local: see
    ``app.core.rate_limit``.
    """

    def __init__(
        self, app: Any, limiter: RateLimiter, *, prefix: str, exempt: tuple[str, ...] = ()
    ) -> None:
        self.app = app
        self.limiter = limiter
        self.prefix = prefix
        self.exempt = exempt

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        path = scope.get("path", "") if scope["type"] == "http" else ""
        if path.startswith(self.prefix) and not path.startswith(self.exempt):
            client = scope.get("client")
            try:
                self.limiter.hit(client[0] if client else "unknown")
            except RateLimitExceededError as exc:
                logger.info(
                    "event=api_error status=429 code=%s cause=%s limit=%s path=%s",
                    ErrorCode.RATE_LIMITED.value,
                    type(exc).__name__,
                    exc.limit,
                    safe_log_value(path),
                )
                response = rate_limited_response(exc.retry_after_seconds)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def rate_limited_response(retry_after_seconds: float) -> JSONResponse:
    return JSONResponse(
        status_code=429,
        content=error_content(ErrorCode.RATE_LIMITED, RATE_LIMITED_MESSAGE),
        headers={"Retry-After": retry_after_header(retry_after_seconds)},
    )


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
