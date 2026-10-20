"""HTTP middleware: request IDs, access logging and a safe error boundary."""

import logging
import time
import uuid
from typing import Any

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.routing import replace_params

from app.core.logging_config import (
    ERROR_CATEGORY_SCOPE_KEY,
    correlation_id_ctx,
    request_id_ctx,
    safe_log_value,
    set_error_category,
    valid_correlation_id,
)
from app.core.metrics import http_method, metrics
from app.core.rate_limit import RateLimiter, RateLimitExceededError, retry_after_header
from app.models.errors import CODES_BY_STATUS, ErrorCode, error_content

logger = logging.getLogger("app.request")

REQUEST_ID_HEADER = "X-Request-ID"
CORRELATION_ID_HEADER = "X-Correlation-ID"
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
    A caller may send its own ``X-Correlation-ID`` (the Streamlit pages send one per
    page action, retries included); when it has the accepted shape it is logged with
    every record of the request, next to the request ID, never instead of it.

    One access record per request: method, route template, status, duration and, for
    an error, its code (``error_category``). The route template
    (``/api/v1/conversations/{session_id}``), not the path, so session and ticket IDs
    stay out of access records; only an unmatched path is logged as sent (escaped).
    Successful requests to ``quiet_routes`` (health checks, polled every few seconds)
    are logged at DEBUG; 5xx responses at WARNING.
    """

    def __init__(self, app: Any, *, quiet_routes: tuple[str, ...] = ()) -> None:
        super().__init__(app)
        self.quiet_routes = frozenset(quiet_routes)

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = uuid.uuid4().hex
        token = request_id_ctx.set(request_id)
        correlation_token = correlation_id_ctx.set(
            valid_correlation_id(request.headers.get(CORRELATION_ID_HEADER))
        )
        start = time.perf_counter()
        try:
            try:
                response = await call_next(request)
            except Exception:
                # Log the full traceback for us, return a generic message to the client.
                route = route_template(request)
                logger.exception("Unhandled error on %s %s", request.method, route)
                logger.error(
                    "event=api_error status=500 code=%s retryable=false method=%s route=%s",
                    ErrorCode.INTERNAL_ERROR.value,
                    request.method,
                    route,
                )
                set_error_category(request.scope, ErrorCode.INTERNAL_ERROR.value)
                response = JSONResponse(
                    status_code=500,
                    content=error_content(ErrorCode.INTERNAL_ERROR, INTERNAL_ERROR_MESSAGE),
                    # Never cached by a proxy or browser: the next request may succeed.
                    headers={"Cache-Control": "no-store"},
                )
            self._log_access(request, response.status_code, time.perf_counter() - start)
            response.headers[REQUEST_ID_HEADER] = request_id
            add_security_headers(response, request.url.path)
            return response
        finally:
            correlation_id_ctx.reset(correlation_token)
            request_id_ctx.reset(token)

    def _log_access(self, request: Request, status_code: int, seconds: float) -> None:
        route = route_template(request)
        fields: dict[str, Any] = {
            "event": "http_request",
            "method": request.method,
            "route": route if route is not None else UNMATCHED_ROUTE,
            "status": status_code,
            "duration_ms": round(seconds * 1000, 1),
        }
        if route is None:
            # Client-controlled: escaped, and still redacted by the formatter.
            fields["path"] = safe_log_value(request.url.path)
        if status_code >= 400:
            fields["error_category"] = request.scope.get(ERROR_CATEGORY_SCOPE_KEY) or (
                CODES_BY_STATUS[status_code].value if status_code in CODES_BY_STATUS else "-"
            )
        _record_request(fields)
        if status_code >= 500:
            level = logging.WARNING
        elif route in self.quiet_routes:
            level = logging.DEBUG
        else:
            level = logging.INFO
        # Never the query string, headers or body: they can carry tokens or customer data.
        logger.log(
            level,
            "%s %s -> %d (%.1f ms)%s",
            fields["method"],
            fields.get("path", fields["route"]),
            status_code,
            fields["duration_ms"],
            f" error={fields['error_category']}" if "error_category" in fields else "",
            extra={"log_fields": fields},
        )


UNMATCHED_ROUTE = "unmatched"


def _record_request(fields: dict[str, Any]) -> None:
    """Request metrics from the access record: route template, never the path."""
    method, route = http_method(fields["method"]), fields["route"]
    metrics.increment("http_requests", method=method, route=route, status=str(fields["status"]))
    metrics.observe_ms("http_request_duration", fields["duration_ms"], method=method, route=route)
    if "error_category" in fields:
        metrics.increment("http_errors", category=fields["error_category"])


def route_template(request: Request) -> str | None:
    """The matched route's full path template, e.g. ``/api/v1/faqs/{faq_id}``, or None
    (no route matched, or routing has not run yet).

    A route inside an included router knows only its own part (``/faqs/{faq_id}``), so
    the router prefixes are recovered from the request: that part, filled in with the
    request's path parameters, ends the actual path, and what comes before it is the
    prefix. Uses only public Starlette helpers, whatever FastAPI does internally.
    """
    route = request.scope.get("route")
    template = getattr(route, "path_format", None)
    if not isinstance(template, str):
        return None
    path = request.scope.get("path", "")
    try:
        filled, _ = replace_params(
            template, getattr(route, "param_convertors", {}), dict(request.path_params)
        )
    except (KeyError, TypeError, ValueError):
        return template
    if filled and path.endswith(filled):
        return path[: len(path) - len(filled)] + template
    return template


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
                set_error_category(scope, ErrorCode.RATE_LIMITED.value)
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
