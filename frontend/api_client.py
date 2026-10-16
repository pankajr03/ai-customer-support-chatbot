"""HTTP client for the customer chat API (Stage 7), used by the Streamlit frontend.

The frontend never answers questions itself: it has no FAQ data, embedding model or
OpenAI key. Everything goes through these endpoints (see docs/chat-api.md):

    GET    /api/v1/health
    POST   /api/v1/conversations                -> session_id + access_token (once)
    POST   /api/v1/conversations/{id}/chat      -> the reply
    GET    /api/v1/conversations/{id}/messages  -> the history, page by page
    POST   /api/v1/conversations/{id}/close
    DELETE /api/v1/conversations/{id}

Responses are parsed into the small models below, which mirror the public contract
rather than importing ``app.models``: importing the backend would pull in its services,
and the contract is what the two sides share, not the code.

Failures become ``ApiError`` with an ``ErrorKind`` the page can branch on. Its string
form never contains the message text, the access token or the server's response body.

Tokens: ``Credentials.access_token`` is a ``SecretStr`` (shown as ``**********``) and is
sent only as ``Authorization: Bearer``, per request. The pooled ``httpx.Client`` holds no
token and refuses cookies, so one client can be shared safely by every browser session.

Logging: method, route name, status, duration and the backend's request ID. Never
message text, tokens or response bodies.
"""

import logging
import time
import uuid
from collections.abc import Callable
from datetime import datetime
from enum import StrEnum
from http.cookiejar import CookieJar, DefaultCookiePolicy
from typing import Any, Literal, Self, TypeVar

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

logger = logging.getLogger(__name__)

HISTORY_PAGE_SIZE = 100  # the backend's maximum page size
MAX_HISTORY_MESSAGES = 1000  # load at most the newest this many (a conversation rarely has 50)
HEALTH_TIMEOUT_SECONDS = 3.0

M = TypeVar("M", bound=BaseModel)


# --- The public contract --------------------------------------------------------------


class Decision(StrEnum):
    ANSWERED = "answered"
    NO_MATCH = "no_match"
    AMBIGUOUS = "ambiguous"
    RETRIEVAL_UNAVAILABLE = "retrieval_unavailable"


class ConversationStatus(StrEnum):
    ACTIVE = "active"
    CLOSED = "closed"
    EXPIRED = "expired"


class _ApiModel(BaseModel):
    # Tolerant of new fields (the backend may add some); strict about the ones used.
    model_config = ConfigDict(extra="ignore", frozen=True)


class Credentials(_ApiModel):
    """What it takes to use one conversation. Keep it out of URLs, logs and the page."""

    session_id: uuid.UUID
    access_token: SecretStr

    def authorization(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.access_token.get_secret_value()}"}


class ConversationCreated(_ApiModel):
    session_id: uuid.UUID
    access_token: SecretStr = Field(min_length=1)
    token_type: Literal["Bearer"]
    status: ConversationStatus
    created_at: datetime
    expires_at: datetime | None

    def credentials(self) -> Credentials:
        return Credentials(session_id=self.session_id, access_token=self.access_token)


class ConversationInfo(_ApiModel):
    session_id: uuid.UUID
    status: ConversationStatus
    created_at: datetime
    updated_at: datetime
    expires_at: datetime | None


class SourceReference(_ApiModel):
    faq_id: str = Field(min_length=1)
    question: str = Field(min_length=1)


class ChatReply(_ApiModel):
    session_id: uuid.UUID
    message_id: uuid.UUID
    client_message_id: uuid.UUID | None
    request_id: str | None
    decision: Decision
    answer: str = Field(min_length=1)
    generation_method: str
    sources: list[SourceReference]
    created_at: datetime
    # From the Idempotent-Replayed header: a retry got the stored reply back.
    replayed: bool = False


class HistoryMessage(_ApiModel):
    message_id: uuid.UUID
    role: Literal["user", "assistant"]
    content: str
    created_at: datetime
    decision: Decision | None
    source_faq_ids: list[str]


class _HistoryPage(_ApiModel):
    session_id: uuid.UUID
    status: ConversationStatus
    messages: list[HistoryMessage]
    total_messages: int = Field(ge=0)
    has_more: bool


class ConversationHistory(_ApiModel):
    """The latest messages of one conversation, oldest first."""

    session_id: uuid.UUID
    status: ConversationStatus
    messages: list[HistoryMessage]
    total_messages: int = Field(ge=0)
    # Earlier messages not loaded because of the history limit (the newest are kept).
    omitted: int = Field(default=0, ge=0)

    @property
    def complete(self) -> bool:
        return self.omitted == 0


class HealthStatus(_ApiModel):
    status: Literal["healthy"]
    service: str


# --- Errors ---------------------------------------------------------------------------


class ErrorKind(StrEnum):
    """What went wrong, in terms the page can act on."""

    BACKEND_UNREACHABLE = "backend_unreachable"  # no connection (backend down, wrong URL)
    TIMEOUT = "timeout"  # sent, but no reply in time: the outcome is unknown
    NETWORK_ERROR = "network_error"  # the connection broke mid-request: outcome unknown
    UNAUTHORIZED = "unauthorized"  # 401: no token sent (a frontend bug)
    FORBIDDEN = "forbidden"  # 403: refused (e.g. by a proxy); the API itself never sends it
    SESSION_NOT_FOUND = "session_not_found"  # 404: unknown conversation or wrong token
    SESSION_EXPIRED = "session_expired"  # 410
    SESSION_CLOSED = "session_closed"  # 409 SESSION_CLOSED
    BUSY = "busy"  # 409 REQUEST_IN_PROGRESS: another message is being answered
    INVALID_MESSAGE = "invalid_message"  # 422 VALIDATION_ERROR or 413: fix the message
    MESSAGE_CONFLICT = "message_conflict"  # 422 DUPLICATE_MESSAGE_CONFLICT: a frontend bug
    RATE_LIMITED = "rate_limited"  # 429
    SERVICE_UNAVAILABLE = "service_unavailable"  # 502/503/504: temporary
    SERVER_ERROR = "server_error"  # 500 and other 5xx
    CLIENT_ERROR = "client_error"  # other 4xx, e.g. a wrong BACKEND_API_PREFIX
    INVALID_RESPONSE = "invalid_response"  # a reply that does not match the contract


# Sending the same message again (same client_message_id) is safe and may succeed.
RETRYABLE = frozenset(
    {
        ErrorKind.BACKEND_UNREACHABLE,
        ErrorKind.TIMEOUT,
        ErrorKind.NETWORK_ERROR,
        ErrorKind.BUSY,
        ErrorKind.RATE_LIMITED,
        ErrorKind.SERVICE_UNAVAILABLE,
    }
)
# Retried automatically by ``send_message``. Timeouts are not: the page has already
# waited the full read timeout, so the customer decides whether to try again.
_AUTO_RETRY = RETRYABLE - {ErrorKind.TIMEOUT}
# The credentials no longer work: the conversation is gone for this page.
ENDS_CONVERSATION = frozenset({ErrorKind.SESSION_NOT_FOUND, ErrorKind.SESSION_EXPIRED})


class ApiError(Exception):
    """A failed API call. Safe to log: it holds no message text, token or body."""

    def __init__(
        self,
        kind: ErrorKind,
        *,
        status_code: int | None = None,
        code: str | None = None,
        request_id: str | None = None,
        retry_after: float | None = None,
        validation_types: tuple[str, ...] = (),
        max_length: int | None = None,
    ) -> None:
        self.kind = kind
        self.status_code = status_code
        self.code = code
        self.request_id = request_id
        self.retry_after = retry_after
        self.validation_types = validation_types
        self.max_length = max_length
        status = f" HTTP {status_code}" if status_code is not None else ""
        super().__init__(f"{kind}{status} {code or ''}".rstrip())

    @property
    def retryable(self) -> bool:
        return self.kind in RETRYABLE

    @property
    def ends_conversation(self) -> bool:
        return self.kind in ENDS_CONVERSATION


def _kind_for(status: int, code: str | None) -> ErrorKind:
    if status == 401:
        return ErrorKind.UNAUTHORIZED
    if status == 403:
        return ErrorKind.FORBIDDEN
    if status == 404:
        # NOT_FOUND (an unknown path) means BACKEND_API_URL or the prefix is wrong.
        if code == "SESSION_NOT_FOUND":
            return ErrorKind.SESSION_NOT_FOUND
        return ErrorKind.CLIENT_ERROR
    if status == 409:
        return ErrorKind.SESSION_CLOSED if code == "SESSION_CLOSED" else ErrorKind.BUSY
    if status == 410:
        return ErrorKind.SESSION_EXPIRED
    if status == 413:
        return ErrorKind.INVALID_MESSAGE
    if status == 422:
        if code == "DUPLICATE_MESSAGE_CONFLICT":
            return ErrorKind.MESSAGE_CONFLICT
        return ErrorKind.INVALID_MESSAGE
    if status == 429:
        return ErrorKind.RATE_LIMITED
    if status in (502, 503, 504):
        return ErrorKind.SERVICE_UNAVAILABLE
    if status >= 500:
        return ErrorKind.SERVER_ERROR
    if status >= 400:
        return ErrorKind.CLIENT_ERROR
    return ErrorKind.INVALID_RESPONSE  # 1xx/3xx: redirects are not followed


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After", "")
    try:
        seconds = float(value)
    except ValueError:
        return None  # absent, or an HTTP date (the API only sends seconds)
    return seconds if seconds >= 0 else None


def error_from_response(response: httpx.Response) -> ApiError:
    """An ``ApiError`` for a non-2xx response, from the API's error envelope if present."""
    error: dict[str, Any] = {}
    try:
        body = response.json()
        if isinstance(body, dict) and isinstance(body.get("error"), dict):
            error = body["error"]
    except ValueError:
        pass  # not JSON (e.g. a proxy's HTML error page)
    code = error.get("code") if isinstance(error.get("code"), str) else None
    request_id = error.get("request_id") or response.headers.get("X-Request-ID")
    types: list[str] = []
    max_length: int | None = None
    details = error.get("details")
    for issue in details if isinstance(details, list) else []:
        if isinstance(issue, dict) and isinstance(issue.get("type"), str):
            types.append(issue["type"])
            ctx = issue.get("ctx")
            if isinstance(ctx, dict) and isinstance(ctx.get("max_length"), int):
                max_length = ctx["max_length"]
    return ApiError(
        _kind_for(response.status_code, code),
        status_code=response.status_code,
        code=code,
        request_id=request_id if isinstance(request_id, str) else None,
        retry_after=_retry_after(response),
        validation_types=tuple(types),
        max_length=max_length,
    )


def _refuse_all_cookies() -> CookieJar:
    # The API sets no cookies; refusing them guarantees nothing is shared between the
    # browser sessions that use one pooled client.
    return CookieJar(policy=DefaultCookiePolicy(allowed_domains=[]))


# --- The client -----------------------------------------------------------------------


class ChatApiClient:
    """Calls the customer chat API. Thread-safe; share one per process.

    Args:
        base_url: The backend's base URL, e.g. ``http://127.0.0.1:8000``.
        api_prefix: The backend's ``API_V1_PREFIX``.
        http_client: A ready-made ``httpx.Client`` whose ``base_url`` points at the
            backend (tests pass Starlette's ``TestClient``). Not closed by ``close()``.
        sleep: Used between retries; tests pass a fake.
    """

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8000",
        *,
        api_prefix: str = "/api/v1",
        connect_timeout: float = 5.0,
        read_timeout: float = 60.0,
        max_retries: int = 2,
        max_retry_wait: float = 5.0,
        http_client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._owns_client = http_client is None
        self._http = http_client or httpx.Client(
            base_url=base_url,
            timeout=httpx.Timeout(read_timeout, connect=connect_timeout),
            cookies=_refuse_all_cookies(),
            follow_redirects=False,  # a redirect could carry the Authorization header away
            headers={"Accept": "application/json"},
        )
        self._prefix = api_prefix
        self._connect_timeout = connect_timeout
        self._max_retries = max_retries
        self._max_retry_wait = max_retry_wait
        self._sleep = sleep

    def close(self) -> None:
        if self._owns_client:
            self._http.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # -- Endpoints ---------------------------------------------------------------------

    def check_health(self) -> HealthStatus:
        """Whether the backend is up. Fast: short timeouts, no retries."""
        timeout = httpx.Timeout(HEALTH_TIMEOUT_SECONDS, connect=self._connect_timeout)
        response = self._send("GET", "/health", route="health", timeout=timeout)
        return self._parse(response, HealthStatus)

    def create_conversation(self) -> ConversationCreated:
        """Start a conversation. Not retried: a lost reply would leave an unused one."""
        response = self._send("POST", "/conversations", route="create")
        created = self._parse(response, ConversationCreated)
        if response.status_code != 201:
            raise self._invalid(response, "create returned no 201")
        return created

    def send_message(
        self, credentials: Credentials, text: str, client_message_id: uuid.UUID
    ) -> ChatReply:
        """Send one customer message and return the reply.

        Retries network errors, ``409 REQUEST_IN_PROGRESS``, ``429`` and ``503`` up to
        ``max_retries`` times, always with the same ``client_message_id``, so the backend
        answers the message at most once. A ``Retry-After`` longer than ``max_retry_wait``
        is not waited for: the error is raised with ``retry_after`` set instead.
        """
        body = {"message": text, "client_message_id": str(client_message_id)}
        path = f"/conversations/{credentials.session_id}/chat"
        attempt = 0
        while True:
            try:
                response = self._send(
                    "POST", path, route="chat", credentials=credentials, json=body
                )
                return self._parse_reply(response, credentials, client_message_id)
            except ApiError as error:
                wait = self._retry_wait(error, attempt)
                if wait is None:
                    raise
                logger.info("chat retry %d after %s in %.1f s", attempt + 1, error.kind, wait)
                self._sleep(wait)
                attempt += 1

    def get_conversation(self, credentials: Credentials) -> ConversationInfo:
        response = self._send(
            "GET", f"/conversations/{credentials.session_id}", route="get", credentials=credentials
        )
        return self._parse(response, ConversationInfo)

    def get_history(
        self, credentials: Credentials, *, max_messages: int = MAX_HISTORY_MESSAGES
    ) -> ConversationHistory:
        """The conversation's stored messages, oldest first, through the authorized
        history endpoint, page by page (``HISTORY_PAGE_SIZE``, the backend's maximum).

        At most the newest ``max_messages`` are loaded; ``omitted`` counts the earlier
        ones. Every page must belong to ``credentials.session_id``, and a message that
        appears on two pages (one was stored while paging) is kept once.
        """
        page = self._history_page(credentials, offset=0)
        total = page.total_messages
        start = max(0, total - max_messages)
        if start:
            page = self._history_page(credentials, offset=start)
        received = 0
        seen: set[uuid.UUID] = set()
        messages: list[HistoryMessage] = []
        while True:
            received += len(page.messages)
            for message in page.messages:
                if message.message_id not in seen:
                    seen.add(message.message_id)
                    messages.append(message)
            if not page.has_more or not page.messages or len(messages) >= max_messages:
                break
            page = self._history_page(credentials, offset=start + received)
        return ConversationHistory(
            session_id=credentials.session_id,
            status=page.status,
            messages=messages[:max_messages],
            total_messages=max(total, page.total_messages),
            omitted=start,
        )

    def _history_page(self, credentials: Credentials, *, offset: int) -> _HistoryPage:
        response = self._send(
            "GET",
            f"/conversations/{credentials.session_id}/messages",
            route="history",
            credentials=credentials,
            params={"offset": offset, "limit": HISTORY_PAGE_SIZE},
        )
        page = self._parse(response, _HistoryPage)
        if page.session_id != credentials.session_id:
            raise self._invalid(response, "history page is for another conversation")
        return page

    def close_conversation(self, credentials: Credentials) -> ConversationInfo:
        response = self._send(
            "POST",
            f"/conversations/{credentials.session_id}/close",
            route="close",
            credentials=credentials,
        )
        return self._parse(response, ConversationInfo)

    def delete_conversation(self, credentials: Credentials) -> None:
        response = self._send(
            "DELETE",
            f"/conversations/{credentials.session_id}",
            route="delete",
            credentials=credentials,
        )
        if response.status_code != 204:
            raise self._invalid(response, "delete returned no 204")

    # -- Internals ---------------------------------------------------------------------

    def _send(
        self,
        method: str,
        path: str,
        *,
        route: str,
        credentials: Credentials | None = None,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        timeout: httpx.Timeout | None = None,
    ) -> httpx.Response:
        """One HTTP request; transport failures become ``ApiError``, non-2xx too."""
        headers = credentials.authorization() if credentials is not None else None
        started = time.perf_counter()
        extra: dict[str, Any] = {} if timeout is None else {"timeout": timeout}
        try:
            response = self._http.request(
                method, self._prefix + path, headers=headers, json=json, params=params, **extra
            )
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            logger.warning("api %s %s failed: backend unreachable", method, route)
            raise ApiError(ErrorKind.BACKEND_UNREACHABLE) from exc
        except httpx.TimeoutException as exc:
            logger.warning("api %s %s failed: timeout", method, route)
            raise ApiError(ErrorKind.TIMEOUT) from exc
        except httpx.TransportError as exc:
            logger.warning("api %s %s failed: %s", method, route, type(exc).__name__)
            raise ApiError(ErrorKind.NETWORK_ERROR) from exc
        elapsed_ms = (time.perf_counter() - started) * 1000
        request_id = response.headers.get("X-Request-ID", "-")
        logger.info(
            "api %s %s -> %d in %.0f ms request_id=%s",
            method,
            route,
            response.status_code,
            elapsed_ms,
            request_id,
        )
        if not response.is_success:
            raise error_from_response(response)
        return response

    def _parse(self, response: httpx.Response, model: type[M]) -> M:
        try:
            return model.model_validate_json(response.content)
        except ValidationError as exc:
            # Only the count: the errors could quote message text from the body.
            raise self._invalid(response, f"{exc.error_count()} contract errors") from None

    def _parse_reply(
        self, response: httpx.Response, credentials: Credentials, client_message_id: uuid.UUID
    ) -> ChatReply:
        reply = self._parse(response, ChatReply)
        if reply.session_id != credentials.session_id or reply.client_message_id not in (
            client_message_id,
            None,
        ):
            raise self._invalid(response, "reply is for another conversation or message")
        if response.headers.get("Idempotent-Replayed") == "true":
            reply = reply.model_copy(update={"replayed": True})
        return reply

    def _invalid(self, response: httpx.Response, reason: str) -> ApiError:
        logger.error("api response does not match the contract: %s", reason)
        return ApiError(
            ErrorKind.INVALID_RESPONSE,
            status_code=response.status_code,
            request_id=response.headers.get("X-Request-ID"),
        )

    def _retry_wait(self, error: ApiError, attempt: int) -> float | None:
        """Seconds to wait before retrying, or None to give up."""
        if error.kind not in _AUTO_RETRY or attempt >= self._max_retries:
            return None
        if error.retry_after is not None:
            return error.retry_after if error.retry_after <= self._max_retry_wait else None
        # No Retry-After: a short, growing pause (0.5 s, 1 s, 1.5 s, ...).
        return min(0.5 * (attempt + 1), self._max_retry_wait)
