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
| Staff endpoint without a valid staff token (Stage 10)  | 401    | STAFF_AUTH_REQUIRED         |
| Staff endpoint while no staff are configured           | 403    | STAFF_ACCESS_DISABLED       |
| Unknown ticket, or not this conversation's             | 404    | TICKET_NOT_FOUND            |
| A status change the workflow does not allow            | 409    | INVALID_STATUS_TRANSITION   |
| The ticket changed since it was read                   | 409    | TICKET_CHANGED              |
| A client_request_id reused for a different request     | 422    | TICKET_REQUEST_CONFLICT     |
| Support storage unavailable                            | 503    | SERVICE_UNAVAILABLE         |
| Staff role does not allow the operation (Stage 11)     | 403    | STAFF_FORBIDDEN             |
| Unknown FAQ on an admin endpoint                       | 404    | FAQ_NOT_FOUND               |
| The FAQ changed since it was read (expected_version)   | 409    | FAQ_VERSION_CONFLICT        |
| Deactivating the last active FAQ                       | 409    | FAQ_LAST_ACTIVE             |
| FAQ storage unavailable, or FAQs not editable here     | 503    | SERVICE_UNAVAILABLE         |
| Another FAQ already has this question                  | 409    | FAQ_DUPLICATE_QUESTION      |
| Assignee change on a resolved or closed ticket         | 409    | INVALID_ASSIGNMENT          |
| Ticket assignee changed since it was read              | 409    | TICKET_CHANGED              |
| A note on an anonymized ticket                         | 409    | TICKET_ANONYMIZED           |

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
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.dependencies import (
    AccessTokenInURLError,
    AccessTokenMissingError,
    FAQManagementUnavailableError,
    StaffAccessDisabledError,
)
from app.core.logging_config import safe_log_value, set_error_category
from app.core.middleware import INTERNAL_ERROR_MESSAGE, RATE_LIMITED_MESSAGE, route_template
from app.core.rate_limit import RateLimitExceededError, retry_after_header
from app.core.staff_auth import StaffAuthenticationError, StaffPermissionError
from app.database.connection import DatabaseError
from app.database.faq_repository import FAQDataError
from app.models.audit import AuditAction, AuditOutcome, AuditResource
from app.models.errors import CODES_BY_STATUS, ErrorCode, ErrorResponse, error_content
from app.repositories.analytics_repository import AnalyticsStoreError
from app.repositories.audit_repository import AuditActor, AuditStoreError
from app.repositories.conversation_repository import (
    ConversationStoreBusyError,
    ConversationStoreError,
)
from app.repositories.faq_repository import (
    DuplicateFAQQuestionError,
    FAQCatalogUnavailableError,
    FAQIdsExhaustedError,
    FAQNotFoundError,
    FAQStoreBusyError,
    FAQStoreError,
    FAQVersionConflictError,
    LastActiveFAQError,
)
from app.repositories.ticket_repository import (
    IdempotencyKeyConflictError,
    InvalidAssignmentError,
    InvalidStatusTransitionError,
    StaleAssignmentError,
    StaleTicketStatusError,
    TicketAnonymizedError,
    TicketNotFoundError,
    TicketStoreError,
)
from app.services.analytics_service import InvalidPeriodError
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
from app.services.escalation_service import (
    ConfirmationRequiredError,
    ConsentRequiredError,
    SupportStoreUnavailableError,
)
from app.services.ticket_workflow import (
    ActorNotPermittedError,
    TransitionNotAllowedError,
    UnknownAssigneeError,
)

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
STAFF_AUTH_MESSAGE = "A valid staff token is required. Send 'Authorization: Bearer <staff token>'."
STAFF_DISABLED_MESSAGE = "Staff operations are disabled on this server."
TICKET_NOT_FOUND_MESSAGE = "Support request not found."
INVALID_TRANSITION_MESSAGE = "This status change is not allowed for the ticket's current status."
TICKET_CHANGED_MESSAGE = "The ticket has changed since it was read. Reload it and try again."
TICKET_CONFLICT_MESSAGE = (
    "This client_request_id was already used for a different request in this conversation. "
    "Use a new ID for a new request."
)
UNKNOWN_ASSIGNEE_MESSAGE = "Tickets can only be assigned to a configured staff member."
NOT_CONFIRMED_MESSAGE = "The request must be confirmed, with consent, before it is sent."
SUPPORT_UNAVAILABLE_MESSAGE = (
    "Support requests are temporarily unavailable. Please try again in a few minutes; "
    "sending the same request again will not create a duplicate."
)
STAFF_FORBIDDEN_MESSAGE = "Your staff role does not allow this operation."
FAQ_NOT_FOUND_MESSAGE = "FAQ not found."
FAQ_VERSION_CONFLICT_MESSAGE = (
    "This FAQ was changed by someone else since you opened it. Reload it, check the "
    "changes and apply your edit again."
)
FAQ_LAST_ACTIVE_MESSAGE = (
    "At least one FAQ must stay active, otherwise customers cannot be answered."
)
FAQ_STORE_UNAVAILABLE_MESSAGE = "FAQ management is temporarily unavailable. Please try again."
FAQ_MANAGEMENT_DISABLED_MESSAGE = "FAQs cannot be edited on this server."
FAQ_IDS_EXHAUSTED_MESSAGE = "No FAQ ID is left for a new FAQ."
ANALYTICS_UNAVAILABLE_MESSAGE = "Analytics are temporarily unavailable. Please try again."
FAQ_DUPLICATE_MESSAGE = (
    "Another FAQ already has this question. Edit that FAQ (it may be inactive) instead of "
    "adding a duplicate."
)
INVALID_ASSIGNMENT_MESSAGE = "Resolved and closed tickets cannot be assigned or reassigned."
TICKET_ANONYMIZED_MESSAGE = "This ticket's personal data was removed; notes cannot be added."
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
    set_error_category(request.scope, code.value)
    fields = {
        "event": "api_error",
        "status": status_code,
        "error_category": code.value,
        "cause": type(cause).__name__ if cause is not None else "-",
        "method": request.method,
        "route": log_route(request),
    }
    logger.log(
        logging.WARNING if status_code >= 500 else logging.INFO,
        "event=api_error status=%d code=%s cause=%s method=%s route=%s",
        *(fields[key] for key in ("status", "error_category", "cause", "method", "route")),
        extra={"log_fields": fields},
    )
    return JSONResponse(
        status_code=status_code,
        content=error_content(code, message, details=details),
        headers=headers,
    )


def log_route(request: Request) -> str:
    """Where a request went, for a log line: the route template (no session or ticket
    IDs), or the escaped path when no route matched."""
    return route_template(request) or safe_log_value(request.url.path)


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
    logger.info("Rejected %s %s: %s", request.method, log_route(request), problems)
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
        log_route(request),
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
        log_route(request),
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
        log_route(request),
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
        log_route(request),
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


# --- Support tickets (Stage 10) -----------------------------------------------------


async def _audit_denied(request: Request, reason: str, exc: Exception) -> None:
    """Record a refused staff request in the audit log (never the token), in a worker
    thread; a failure to record is logged by the repository and ignored here."""
    audit = getattr(request.app.state, "audit_repository", None)
    if audit is None:
        return
    staff_id = getattr(exc, "staff_id", None)
    permission = getattr(exc, "permission", None)
    details: dict[str, Any] = {"reason": reason, "method": request.method}
    if permission is not None:
        details["permission"] = permission.value
    await run_in_threadpool(
        audit.record,
        actor=AuditActor.staff(staff_id) if staff_id else AuditActor.unauthenticated(),
        action=AuditAction.ACCESS_DENIED,
        resource_type=AuditResource.ENDPOINT,
        resource_id=request.url.path[:200],
        outcome=AuditOutcome.DENIED,
        details=details,
    )


async def _staff_auth_required(request: Request, exc: Exception) -> Response:
    await _audit_denied(request, "invalid_or_missing_staff_token", exc)
    return error_response(
        request,
        status.HTTP_401_UNAUTHORIZED,
        ErrorCode.STAFF_AUTH_REQUIRED,
        STAFF_AUTH_MESSAGE,
        cause=exc,
        headers={**NO_STORE, "WWW-Authenticate": "Bearer"},
    )


async def _staff_access_disabled(request: Request, exc: Exception) -> Response:
    await _audit_denied(request, "no_staff_configured", exc)
    return error_response(
        request,
        status.HTTP_403_FORBIDDEN,
        ErrorCode.STAFF_ACCESS_DISABLED,
        STAFF_DISABLED_MESSAGE,
        cause=exc,
        headers=NO_STORE,
    )


async def _ticket_not_found(request: Request, exc: Exception) -> Response:
    # Unknown tickets and other conversations' tickets look the same.
    return error_response(
        request,
        status.HTTP_404_NOT_FOUND,
        ErrorCode.TICKET_NOT_FOUND,
        TICKET_NOT_FOUND_MESSAGE,
        cause=exc,
        headers=NO_STORE,
    )


async def _invalid_transition(request: Request, exc: Exception) -> Response:
    return error_response(
        request,
        status.HTTP_409_CONFLICT,
        ErrorCode.INVALID_STATUS_TRANSITION,
        INVALID_TRANSITION_MESSAGE,
        cause=exc,
        headers=NO_STORE,
    )


async def _ticket_changed(request: Request, exc: Exception) -> Response:
    return error_response(
        request,
        status.HTTP_409_CONFLICT,
        ErrorCode.TICKET_CHANGED,
        TICKET_CHANGED_MESSAGE,
        cause=exc,
        headers=NO_STORE,
    )


async def _ticket_conflict(request: Request, exc: Exception) -> Response:
    return error_response(
        request,
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        ErrorCode.TICKET_REQUEST_CONFLICT,
        TICKET_CONFLICT_MESSAGE,
        cause=exc,
        headers=NO_STORE,
    )


async def _ticket_validation(request: Request, exc: Exception) -> Response:
    message = (
        UNKNOWN_ASSIGNEE_MESSAGE if isinstance(exc, UnknownAssigneeError) else NOT_CONFIRMED_MESSAGE
    )
    return error_response(
        request,
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        ErrorCode.VALIDATION_ERROR,
        message,
        cause=exc,
        headers=NO_STORE,
    )


async def _support_unavailable(request: Request, exc: Exception) -> Response:
    logger.error(
        "Support storage unavailable on %s %s: %s",
        request.method,
        log_route(request),
        type(exc).__name__,
    )
    return error_response(
        request,
        status.HTTP_503_SERVICE_UNAVAILABLE,
        ErrorCode.SERVICE_UNAVAILABLE,
        SUPPORT_UNAVAILABLE_MESSAGE,
        cause=exc,
        headers=NO_STORE,
    )


# --- Admin dashboard (Stage 11) -------------------------------------------------------


async def _staff_forbidden(request: Request, exc: Exception) -> Response:
    # Which permission was missing is logged, not returned.
    permission = getattr(exc, "permission", None)
    logger.info("Staff role lacks permission %s", getattr(permission, "value", "-"))
    await _audit_denied(request, "role_not_permitted", exc)
    return error_response(
        request,
        status.HTTP_403_FORBIDDEN,
        ErrorCode.STAFF_FORBIDDEN,
        STAFF_FORBIDDEN_MESSAGE,
        cause=exc,
        headers=NO_STORE,
    )


async def _faq_not_found(request: Request, exc: Exception) -> Response:
    return error_response(
        request,
        status.HTTP_404_NOT_FOUND,
        ErrorCode.FAQ_NOT_FOUND,
        FAQ_NOT_FOUND_MESSAGE,
        cause=exc,
        headers=NO_STORE,
    )


async def _faq_conflict(request: Request, exc: Exception) -> Response:
    if isinstance(exc, LastActiveFAQError):
        code, message = ErrorCode.FAQ_LAST_ACTIVE, FAQ_LAST_ACTIVE_MESSAGE
    elif isinstance(exc, DuplicateFAQQuestionError):
        code, message = ErrorCode.FAQ_DUPLICATE_QUESTION, FAQ_DUPLICATE_MESSAGE
    else:
        code, message = ErrorCode.FAQ_VERSION_CONFLICT, FAQ_VERSION_CONFLICT_MESSAGE
    return error_response(
        request, status.HTTP_409_CONFLICT, code, message, cause=exc, headers=NO_STORE
    )


async def _ticket_conflict_409(request: Request, exc: Exception) -> Response:
    if isinstance(exc, InvalidAssignmentError):
        code, message = ErrorCode.INVALID_ASSIGNMENT, INVALID_ASSIGNMENT_MESSAGE
    else:
        code, message = ErrorCode.TICKET_ANONYMIZED, TICKET_ANONYMIZED_MESSAGE
    return error_response(
        request, status.HTTP_409_CONFLICT, code, message, cause=exc, headers=NO_STORE
    )


async def _invalid_period(request: Request, exc: Exception) -> Response:
    # Our own fixed sentences (no input echoed).
    return error_response(
        request,
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        ErrorCode.VALIDATION_ERROR,
        f"Invalid period: {exc}.",
        cause=exc,
        headers=NO_STORE,
    )


async def _faq_store_unavailable(request: Request, exc: Exception) -> Response:
    logger.error(
        "FAQ storage unavailable on %s %s: %s",
        request.method,
        log_route(request),
        type(exc).__name__,
    )
    if isinstance(exc, FAQCatalogUnavailableError):
        message = FAQ_UNAVAILABLE_MESSAGE  # customer-facing reads (search, FAQs)
    elif isinstance(exc, FAQManagementUnavailableError):
        message = FAQ_MANAGEMENT_DISABLED_MESSAGE
    elif isinstance(exc, FAQIdsExhaustedError):
        message = FAQ_IDS_EXHAUSTED_MESSAGE
    elif isinstance(exc, AnalyticsStoreError | AuditStoreError):
        message = ANALYTICS_UNAVAILABLE_MESSAGE
    else:
        message = FAQ_STORE_UNAVAILABLE_MESSAGE
    headers = dict(NO_STORE)
    if isinstance(exc, FAQStoreBusyError):
        headers["Retry-After"] = str(STORAGE_BUSY_RETRY_SECONDS)
    return error_response(
        request,
        status.HTTP_503_SERVICE_UNAVAILABLE,
        ErrorCode.SERVICE_UNAVAILABLE,
        message,
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
        (StaffAuthenticationError, _staff_auth_required),
        (StaffAccessDisabledError, _staff_access_disabled),
        (TicketNotFoundError, _ticket_not_found),
        (TransitionNotAllowedError, _invalid_transition),
        (InvalidStatusTransitionError, _invalid_transition),
        (ActorNotPermittedError, _invalid_transition),
        (StaleTicketStatusError, _ticket_changed),
        (IdempotencyKeyConflictError, _ticket_conflict),
        (UnknownAssigneeError, _ticket_validation),
        (ConfirmationRequiredError, _ticket_validation),
        (ConsentRequiredError, _ticket_validation),
        (SupportStoreUnavailableError, _support_unavailable),
        (TicketStoreError, _support_unavailable),
        (StaffPermissionError, _staff_forbidden),
        (FAQNotFoundError, _faq_not_found),
        (FAQVersionConflictError, _faq_conflict),
        (LastActiveFAQError, _faq_conflict),
        (DuplicateFAQQuestionError, _faq_conflict),
        (StaleAssignmentError, _ticket_changed),
        (InvalidAssignmentError, _ticket_conflict_409),
        (TicketAnonymizedError, _ticket_conflict_409),
        (FAQStoreError, _faq_store_unavailable),
        (FAQIdsExhaustedError, _faq_store_unavailable),
        (FAQManagementUnavailableError, _faq_store_unavailable),
        (AnalyticsStoreError, _faq_store_unavailable),
        (AuditStoreError, _faq_store_unavailable),
        (InvalidPeriodError, _invalid_period),
    ]
    for exception, handler in handlers:
        app.add_exception_handler(exception, handler)
