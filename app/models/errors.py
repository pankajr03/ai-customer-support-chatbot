"""The error body every API endpoint returns (Stage 7).

```json
{
  "error": {
    "code": "SESSION_EXPIRED",
    "message": "Your conversation has expired. Please start a new conversation.",
    "request_id": "6f1c0e3a9b2d4c7e8a5f1b3d2c4e6a8b"
  }
}
```

- ``code`` is stable: clients branch on it. ``message`` is a short, safe sentence for
  people and may change.
- ``request_id`` matches the ``X-Request-ID`` header and the server logs.
- ``details`` appears only on ``VALIDATION_ERROR``: FastAPI's field errors (``loc``,
  ``type``, ``msg`` and safe limits in ``ctx``), never the submitted values.

Nothing else is ever included: no exception types, stack traces, file paths, SQL,
prompts or credentials. Unknown sessions and wrong tokens give the same
``SESSION_NOT_FOUND``, so a caller cannot tell whether another customer's session
exists.
"""

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.core.logging_config import request_id_ctx


class ErrorCode(StrEnum):
    """Stable error codes. The HTTP status for each is fixed (see ``STATUS``)."""

    VALIDATION_ERROR = "VALIDATION_ERROR"  # 422 (400 for an unreadable body or token in URL)
    UNAUTHORIZED_SESSION = "UNAUTHORIZED_SESSION"  # 401: no Authorization: Bearer token
    SESSION_NOT_FOUND = "SESSION_NOT_FOUND"  # 404: unknown session OR wrong token
    SESSION_EXPIRED = "SESSION_EXPIRED"  # 410
    SESSION_CLOSED = "SESSION_CLOSED"  # 409
    DUPLICATE_MESSAGE_CONFLICT = "DUPLICATE_MESSAGE_CONFLICT"  # 422: retry key reused
    REQUEST_IN_PROGRESS = "REQUEST_IN_PROGRESS"  # 409: another message is being answered
    RATE_LIMITED = "RATE_LIMITED"  # 429: a process-local rate limit (app.core.rate_limit)
    NOT_FOUND = "NOT_FOUND"  # 404: unknown path or FAQ
    METHOD_NOT_ALLOWED = "METHOD_NOT_ALLOWED"  # 405
    PAYLOAD_TOO_LARGE = "PAYLOAD_TOO_LARGE"  # 413
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"  # 503: storage, knowledge base or search
    INTERNAL_ERROR = "INTERNAL_ERROR"  # 500
    # Support tickets (Stage 10)
    STAFF_AUTH_REQUIRED = "STAFF_AUTH_REQUIRED"  # 401: no valid staff token
    STAFF_ACCESS_DISABLED = "STAFF_ACCESS_DISABLED"  # 403: no staff configured on the server
    TICKET_NOT_FOUND = "TICKET_NOT_FOUND"  # 404: unknown ticket, or not this conversation's
    INVALID_STATUS_TRANSITION = "INVALID_STATUS_TRANSITION"  # 409: not allowed from here
    TICKET_CHANGED = "TICKET_CHANGED"  # 409: changed since it was read (expected_status)
    TICKET_REQUEST_CONFLICT = "TICKET_REQUEST_CONFLICT"  # 422: client_request_id reused
    # Admin dashboard (Stage 11)
    STAFF_FORBIDDEN = "STAFF_FORBIDDEN"  # 403: the staff member's role does not allow it
    FAQ_NOT_FOUND = "FAQ_NOT_FOUND"  # 404: unknown FAQ (admin endpoints)
    FAQ_VERSION_CONFLICT = "FAQ_VERSION_CONFLICT"  # 409: changed since it was read
    FAQ_LAST_ACTIVE = "FAQ_LAST_ACTIVE"  # 409: would leave no active FAQ
    FAQ_DUPLICATE_QUESTION = "FAQ_DUPLICATE_QUESTION"  # 409: another FAQ asks the same
    INVALID_ASSIGNMENT = "INVALID_ASSIGNMENT"  # 409: resolved/closed tickets keep theirs
    TICKET_ANONYMIZED = "TICKET_ANONYMIZED"  # 409: personal data removed; no new notes


# Codes for HTTP errors raised by the framework or by ``HTTPException`` in routes.
CODES_BY_STATUS: dict[int, ErrorCode] = {
    400: ErrorCode.VALIDATION_ERROR,
    401: ErrorCode.UNAUTHORIZED_SESSION,
    404: ErrorCode.NOT_FOUND,
    405: ErrorCode.METHOD_NOT_ALLOWED,
    413: ErrorCode.PAYLOAD_TOO_LARGE,
    422: ErrorCode.VALIDATION_ERROR,
    429: ErrorCode.RATE_LIMITED,
    503: ErrorCode.SERVICE_UNAVAILABLE,
}


class ValidationIssue(BaseModel):
    """One invalid field, as FastAPI reports it, without the submitted value."""

    model_config = ConfigDict(extra="forbid")

    loc: list[str | int] = Field(description='Where, e.g. ["body", "message"].')
    type: str = Field(description="The rule that failed, e.g. string_too_long.")
    msg: str = Field(description="What is wrong, for developers.")
    ctx: dict[str, Any] | None = Field(
        default=None, description='Limits of the rule, e.g. {"max_length": 4000}.'
    )


class ErrorBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: ErrorCode = Field(description="Stable error code to branch on.")
    message: str = Field(description="A short, safe explanation for people (may change).")
    request_id: str | None = Field(
        description="This request's ID (as in X-Request-ID), for support."
    )
    details: list[ValidationIssue] | None = Field(
        default=None, description="VALIDATION_ERROR only: the invalid fields."
    )


class ErrorResponse(BaseModel):
    """The body of every error response."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "error": {
                        "code": "SESSION_EXPIRED",
                        "message": "Your conversation has expired. Please start a new "
                        "conversation.",
                        "request_id": "6f1c0e3a9b2d4c7e8a5f1b3d2c4e6a8b",
                    }
                }
            ]
        },
    )

    error: ErrorBody


def current_request_id() -> str | None:
    request_id = request_id_ctx.get()
    return None if request_id == "-" else request_id


def error_content(
    code: ErrorCode, message: str, *, details: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """The JSON body for an error, with the current request ID."""
    body: dict[str, Any] = {
        "code": code.value,
        "message": message,
        "request_id": current_request_id(),
    }
    if details is not None:
        body["details"] = details
    return {"error": body}


def error_example(code: ErrorCode, message: str) -> dict[str, Any]:
    """An OpenAPI example of an error body."""
    return {"error": {"code": code.value, "message": message, "request_id": "<id>"}}
