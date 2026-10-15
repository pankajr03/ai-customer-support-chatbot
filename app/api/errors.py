"""Application-wide error handling: one error shape for every endpoint.

Every error response is an ``ErrorResponse`` (``app.models.errors``)::

    {"error": {"code": "SESSION_EXPIRED", "message": "...", "request_id": "..."}}

| Situation                                              | Status | Code                        |
|--------------------------------------------------------|--------|-----------------------------|
| Invalid body, parameters or headers                    | 422    | VALIDATION_ERROR (+details) |
| Unreadable body; access token sent in the URL          | 400    | VALIDATION_ERROR            |
| No ``Authorization: Bearer`` token                     | 401    | UNAUTHORIZED_SESSION        |
| Unknown session, malformed ID or wrong token (same!)   | 404    | SESSION_NOT_FOUND           |
| Conversation expired                                   | 410    | SESSION_EXPIRED             |
| Conversation closed                                    | 409    | SESSION_CLOSED              |
| Another message (or this one) is still being answered  | 409    | REQUEST_IN_PROGRESS         |
| A retry key reused for a different request             | 422    | DUPLICATE_MESSAGE_CONFLICT  |
| Too many requests (process-local limits)               | 429    | RATE_LIMITED                |
| Unknown path or FAQ                                    | 404    | NOT_FOUND                   |
| Wrong HTTP method                                      | 405    | METHOD_NOT_ALLOWED          |
| Body larger than MAX_REQUEST_BODY_BYTES                | 413    | PAYLOAD_TOO_LARGE           |
| Storage, knowledge base or semantic search unavailable | 503    | SERVICE_UNAVAILABLE         |
| Anything unexpected (``RequestContextMiddleware``)     | 500    | INTERNAL_ERROR              |

A legitimate no-match answer is not an error: it is a 200 with ``decision: no_match``.

Logging: every error response logs one ``event=api_error`` line with the status, the
code and the exception type (the diagnostic category), never values: no customer text,
tokens or SQL parameters. Expected failures with a useful cause (FAQ file, model) also
log that cause; validation failures log the failing fields and rule types.

Conversation errors carry ``Cache-Control: no-store``.
"""

import logging
import math
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.dependencies import AccessTokenInURLError, AccessTokenMissingError
from app.core.logging_config import safe_log_value
from app.core.middleware import INTERNAL_ERROR_MESSAGE, RATE_LIMITED_MESSAGE
from app.core.rate_limit import RateLimitExceededError, retry_after_header
from app.database.connection import DatabaseError
from app.database.faq_repository import FAQDataError
from app.models.errors import CODES_BY_STATUS, ErrorCode, ErrorResponse, error_content
from app.repositories.conversation_repository import (
    ConversationStoreBusyError,
    ConversationStoreError,
)
from app.services.conversation_service import (
    ConversationBusyError,
    ConversationChangedError,
    ConversationExpiredError,
    ConversationNotActiveError,
    ConversationNotFoundError,
)
from app.services.customer_chat_service import (
    ClientMessageIdMismatchError,
    CustomerMessageTooLongError,
    IdempotencyKeyMismatchError,
    KnowledgeBaseUnavailableError,
)
from app.services.embedding_service import EmbeddingError

logger = logging.getLogger("app.errors")

VALIDATION_MESSAGE = "The request is not valid. See details for the fields to fix."
UNREADABLE_BODY_MESSAGE = "The request body could not be read. Send JSON encoded as UTF-8."
FAQ_UNAVAILABLE_MESSAGE = (
    "Our help articles are temporarily unavailable. Please try again in a few minutes."
)
SEMANTIC_UNAVAILABLE_MESSAGE = "Semantic search is temporarily unavailable."
STORAGE_UNAVAILABLE_MESSAGE = (
    "Conversations are temporarily unavailable. Please try again in a few minutes."
)
SESSION_NOT_FOUND_MESSAGE = "Conversation not found. Please start a new conversation."
SESSION_EXPIRED_MESSAGE = "Your conversation has expired. Please start a new conversation."
SESSION_CLOSED_MESSAGE = "This conversation has been closed. Please start a new conversation."
BUSY_MESSAGE = (
    "Another message in this conversation is still being answered. Please try again in a moment."
)
DUPLICATE_IN_PROGRESS_MESSAGE = (
    "This message is still being answered. Please try again in a moment to get the reply."
)
CHANGED_MESSAGE = (
    "Another message in this conversation was answered at the same time. "
    "Please send your message again."
)
DUPLICATE_MESSAGE_MESSAGE = (
    "This client_message_id was already used for a different message in this conversation."
)
KEYS_DISAGREE_MESSAGE = (
    "client_message_id and the Idempotency-Key header must be the same when both are sent."
)
MISSING_AUTHORIZATION_MESSAGE = (
    "Missing conversation access token. Send 'Authorization: Bearer <token>'."
)
CREDENTIALS_IN_URL_MESSAGE = "Send the access token in the Authorization header, never in the URL."
STORAGE_BUSY_RETRY_SECONDS = 1
NO_STORE = {"Cache-Control": "no-store"}
# ``ctx`` entries that describe a rule's limits; anything else (e.g. a decoding error
# message or an exception object) is dropped from validation details.
_SAFE_CTX_KEYS = frozenset(
    {"max_length", "min_length", "ge", "gt", "le", "lt", "pattern", "expected", "multiple_of"}
)


# The 422 every route can return, for OpenAPI (replaces FastAPI's default schema, which
# describes its own body shape). Passed to ``include_router`` in ``create_app``.
VALIDATION_RESPONSES: dict[int | str, dict[str, Any]] = {
    status.HTTP_422_UNPROCESSABLE_CONTENT: {
        "model": ErrorResponse,
        "description": "Invalid request. `error.details` lists the fields to fix, with the "
        "rule that failed (never the submitted values).",
    }
}


def error_response(
    request: Request,
    status_code: int,
    code: ErrorCode,
    message: str,
    *,
    cause: BaseException | None = None,
    headers: dict[str, str] | None = None,
    details: list[dict[str, Any]] | None = None,
) -> JSONResponse:
    """The ``ErrorResponse`` for a failure, logged with its diagnostic category."""
    logger.log(
        logging.WARNING if status_code >= 500 else logging.INFO,
        "event=api_error status=%d code=%s cause=%s method=%s path=%s",
        status_code,
        code.value,
        type(cause).__name__ if cause is not None else "-",
        request.method,
        safe_log_value(request.url.path),
    )
    return JSONResponse(
        status_code=status_code,
        content=error_content(code, message, details=details),
        headers=headers,
    )


# --- Validation -----------------------------------------------------------------------


def validation_details(errors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """FastAPI's field errors without the submitted values (``input``) and with only
    the limits of each rule in ``ctx``. Strings are made UTF-8 encodable."""
    details = []
    for error in jsonable_encoder(errors):
        item: dict[str, Any] = {
            "loc": error.get("loc", []),
            "type": error.get("type", "invalid"),
            "msg": error.get("msg", ""),
        }
        ctx = {k: v for k, v in (error.get("ctx") or {}).items() if k in _SAFE_CTX_KEYS}
        if ctx:
            item["ctx"] = ctx
        details.append(_encodable(item))
    return details


def _encodable(value: Any) -> Any:
    """``value`` with every string made UTF-8 encodable (surrogates backslash-escaped):
    an unpaired surrogate such as "\\ud800" would otherwise fail rendering with a 500."""
    if isinstance(value, str):
        return value.encode("utf-8", "backslashreplace").decode("utf-8")
    if isinstance(value, list):
        return [_encodable(item) for item in value]
    if isinstance(value, dict):
        return {key: _encodable(item) for key, item in value.items()}
    return value


async def _validation_error(request: Request, exc: RequestValidationError) -> Response:
    problems = "; ".join(
        f"{'.'.join(str(part) for part in error['loc'])} ({error['type']})"
        for error in exc.errors()
    )
    logger.info("Rejected %s %s: %s", request.method, safe_log_value(request.url.path), problems)
    return error_response(
        request,
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        ErrorCode.VALIDATION_ERROR,
        VALIDATION_MESSAGE,
        cause=exc,
        details=validation_details(list(exc.errors())),
    )


async def _message_too_long(request: Request, exc: Exception) -> Response:
    # The configured limit, reported like the model's own length check.
    limit = getattr(exc, "limit", 0)
    logger.info(
        "Rejected %s %s: body.message (string_too_long)",
        request.method,
        safe_log_value(request.url.path),
    )
    detail = {
        "loc": ["body", "message"],
        "type": "string_too_long",
        "msg": f"String should have at most {limit} characters",
        "ctx": {"max_length": limit},
    }
    return error_response(
        request,
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        ErrorCode.VALIDATION_ERROR,
        VALIDATION_MESSAGE,
        cause=exc,
        headers=NO_STORE,
        details=[detail],
    )


# --- Framework HTTP errors ------------------------------------------------------------


async def _http_error(request: Request, exc: Exception) -> Response:
    """404 (unknown path, unknown FAQ), 405, 400 (unreadable body) and any other
    ``HTTPException``: the same envelope, keeping headers such as ``Allow``."""
    status_code = getattr(exc, "status_code", 500)
    code = CODES_BY_STATUS.get(
        status_code, ErrorCode.INTERNAL_ERROR if status_code >= 500 else ErrorCode.VALIDATION_ERROR
    )
    detail = getattr(exc, "detail", None)
    if status_code == status.HTTP_400_BAD_REQUEST:
        message = UNREADABLE_BODY_MESSAGE  # FastAPI: "There was an error parsing the body"
    elif isinstance(detail, str) and detail:
        message = detail if detail.endswith(".") else f"{detail}."
    else:
        message = INTERNAL_ERROR_MESSAGE if status_code >= 500 else VALIDATION_MESSAGE
    return error_response(
        request, status_code, code, message, cause=exc, headers=getattr(exc, "headers", None)
    )


# --- Knowledge base and search --------------------------------------------------------


async def _faq_data_unavailable(request: Request, exc: Exception) -> Response:
    # A chat turn raises KnowledgeBaseUnavailableError from the FAQDataError: log that.
    cause = exc.__cause__ if isinstance(exc, KnowledgeBaseUnavailableError) else exc
    logger.error(
        "FAQ data unavailable on %s %s: %s: %s",
        request.method,
        safe_log_value(request.url.path),
        type(cause).__name__,
        cause,
    )
    return error_response(
        request,
        status.HTTP_503_SERVICE_UNAVAILABLE,
        ErrorCode.SERVICE_UNAVAILABLE,
        FAQ_UNAVAILABLE_MESSAGE,
        cause=cause,
        headers=NO_STORE if isinstance(exc, KnowledgeBaseUnavailableError) else None,
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
    return error_response(
        request,
        status.HTTP_503_SERVICE_UNAVAILABLE,
        ErrorCode.SERVICE_UNAVAILABLE,
        SEMANTIC_UNAVAILABLE_MESSAGE,
        cause=exc,
        headers=headers,
    )


# --- Conversations --------------------------------------------------------------------


async def _access_token_missing(request: Request, exc: Exception) -> Response:
    return error_response(
        request,
        status.HTTP_401_UNAUTHORIZED,
        ErrorCode.UNAUTHORIZED_SESSION,
        MISSING_AUTHORIZATION_MESSAGE,
        cause=exc,
        headers={**NO_STORE, "WWW-Authenticate": "Bearer"},
    )


async def _access_token_in_url(request: Request, exc: Exception) -> Response:
    return error_response(
        request,
        status.HTTP_400_BAD_REQUEST,
        ErrorCode.VALIDATION_ERROR,
        CREDENTIALS_IN_URL_MESSAGE,
        cause=exc,
        headers=NO_STORE,
    )


async def _session_not_found(request: Request, exc: Exception) -> Response:
    # Unknown session, malformed ID and wrong token all end here, identically, so a
    # caller cannot tell whether someone else's session exists.
    return error_response(
        request,
        status.HTTP_404_NOT_FOUND,
        ErrorCode.SESSION_NOT_FOUND,
        SESSION_NOT_FOUND_MESSAGE,
        cause=exc,
        headers=NO_STORE,
    )


async def _session_closed(request: Request, exc: Exception) -> Response:
    return error_response(
        request,
        status.HTTP_409_CONFLICT,
        ErrorCode.SESSION_CLOSED,
        SESSION_CLOSED_MESSAGE,
        cause=exc,
        headers=NO_STORE,
    )


async def _session_expired(request: Request, exc: Exception) -> Response:
    return error_response(
        request,
        status.HTTP_410_GONE,
        ErrorCode.SESSION_EXPIRED,
        SESSION_EXPIRED_MESSAGE,
        cause=exc,
        headers=NO_STORE,
    )


async def _request_in_progress(request: Request, exc: Exception) -> Response:
    duplicate = getattr(exc, "duplicate", False)
    retry_after = math.ceil(getattr(exc, "retry_after_seconds", 1))
    return error_response(
        request,
        status.HTTP_409_CONFLICT,
        ErrorCode.REQUEST_IN_PROGRESS,
        DUPLICATE_IN_PROGRESS_MESSAGE if duplicate else BUSY_MESSAGE,
        cause=exc,
        headers={**NO_STORE, "Retry-After": str(retry_after)},
    )


async def _conversation_changed(request: Request, exc: Exception) -> Response:
    return error_response(
        request,
        status.HTTP_409_CONFLICT,
        ErrorCode.REQUEST_IN_PROGRESS,
        CHANGED_MESSAGE,
        cause=exc,
        headers=NO_STORE,
    )


async def _duplicate_message(request: Request, exc: Exception) -> Response:
    keys_disagree = isinstance(exc, ClientMessageIdMismatchError)
    return error_response(
        request,
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        ErrorCode.DUPLICATE_MESSAGE_CONFLICT,
        KEYS_DISAGREE_MESSAGE if keys_disagree else DUPLICATE_MESSAGE_MESSAGE,
        cause=exc,
        headers=NO_STORE,
    )


async def _rate_limited(request: Request, exc: Exception) -> Response:
    retry_after = getattr(exc, "retry_after_seconds", 1.0)
    return error_response(
        request,
        status.HTTP_429_TOO_MANY_REQUESTS,
        ErrorCode.RATE_LIMITED,
        RATE_LIMITED_MESSAGE,
        cause=exc,
        headers={**NO_STORE, "Retry-After": retry_after_header(retry_after)},
    )


async def _storage_unavailable(request: Request, exc: Exception) -> Response:
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
    return error_response(
        request,
        status.HTTP_503_SERVICE_UNAVAILABLE,
        ErrorCode.SERVICE_UNAVAILABLE,
        STORAGE_UNAVAILABLE_MESSAGE,
        cause=exc,
        headers=headers,
    )


def register_exception_handlers(app: FastAPI) -> None:
    """Attach the handlers above to an application. Unexpected exceptions become a 500
    ``INTERNAL_ERROR`` in ``RequestContextMiddleware``."""
    handlers: list[tuple[type[Exception], Any]] = [
        (RequestValidationError, _validation_error),
        (StarletteHTTPException, _http_error),
        (CustomerMessageTooLongError, _message_too_long),
        (FAQDataError, _faq_data_unavailable),
        (KnowledgeBaseUnavailableError, _faq_data_unavailable),
        (EmbeddingError, _semantic_search_unavailable),
        (AccessTokenMissingError, _access_token_missing),
        (AccessTokenInURLError, _access_token_in_url),
        (ConversationNotFoundError, _session_not_found),
        (ConversationNotActiveError, _session_closed),
        (ConversationExpiredError, _session_expired),
        (ConversationBusyError, _request_in_progress),
        (ConversationChangedError, _conversation_changed),
        (ClientMessageIdMismatchError, _duplicate_message),
        (IdempotencyKeyMismatchError, _duplicate_message),
        (RateLimitExceededError, _rate_limited),
        (ConversationStoreError, _storage_unavailable),
        (DatabaseError, _storage_unavailable),
    ]
    for exception, handler in handlers:
        app.add_exception_handler(exception, handler)
