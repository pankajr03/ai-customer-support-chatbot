"""Logging configuration built on Python's standard ``logging`` module.

Design:
- One handler, attached to the root logger only. Every other logger (ours,
  uvicorn's, third-party libraries') propagates up to it, so each record is
  written exactly once and in the same format.
- ``configure_logging`` is idempotent: ``dictConfig`` replaces existing root
  handlers, so calling it repeatedly (tests, reloads) never duplicates output.
- Two formats (``LOG_FORMAT``): ``text``, one readable line per record, for
  development; ``json``, one JSON object per line with fixed field names, for Docker
  and any log collector. A JSON record is always one line, tracebacks included, so
  Docker's line-based log driver never splits it. Field reference: docs/logging.md.
- Every record carries the service name, the request ID (generated server-side for
  each request) and the correlation ID (a validated ``X-Correlation-ID`` from the
  caller, e.g. the Streamlit page), via ContextVars.
- Structured fields are passed as ``extra={"log_fields": {...}}``: in JSON they become
  top-level keys (they never replace the standard ones); in text the message already
  says the same in words.
- Output is passed through redaction as a last line of defence against secrets and
  contact details reaching the logs. The first line of defence is simply not logging
  sensitive values (see ``Settings.safe_summary`` and the request middleware, which
  logs the route template but never the query string, headers or body).
- Tracebacks keep their frames (file, line, code) but not exception messages,
  which can quote customer text (a validation error's ``input_value=...``) or SQL
  parameters: each message is replaced by ``[message not logged]``. With
  ``DEBUG=true`` (never allowed in production) messages are kept.

Usage in any module:
    logger = logging.getLogger(__name__)
"""

import json
import logging
import logging.config
import re
import traceback
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any, Literal

# Hold the IDs of the request currently being handled. ContextVar keeps the values
# isolated between concurrent requests running on the same event loop.
request_id_ctx: ContextVar[str] = ContextVar("request_id", default="-")
correlation_id_ctx: ContextVar[str | None] = ContextVar("correlation_id", default=None)

LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | req=%(request_id)s | %(message)s"
DEFAULT_SERVICE = "ai-customer-support-chatbot"

REDACTED = "[REDACTED]"

# A caller's correlation ID is logged only in this shape (a UUID or hex ID, for example):
# anything else could forge or garble log lines, so it is ignored.
CORRELATION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{7,63}$")

# Key in the ASGI scope where error handlers leave the error code of the response, so the
# access record can carry it as ``error_category``.
ERROR_CATEGORY_SCOPE_KEY = "app.error_category"

# Names every JSON record may carry; ``log_fields`` cannot replace them.
STANDARD_FIELDS = (
    "timestamp",
    "level",
    "service",
    "logger",
    "request_id",
    "correlation_id",
    "event",
    "message",
)

# (pattern, replacement) pairs applied to every formatted log line.
_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Authorization: Bearer <token>
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+"), f"Bearer {REDACTED}"),
    # OpenAI-style secret keys
    (re.compile(r"\bsk-[A-Za-z0-9_-]{8,}"), REDACTED),
    # Conversation and staff tokens ("cst_"/"stf_" + 43 URL-safe characters), even
    # without "Bearer" (e.g. pasted into a message or an exception text)
    (re.compile(r"\b(?:cst|stf)_[A-Za-z0-9_-]{16,}"), REDACTED),
    # password=..., "token": "...", api_key: ..., etc.
    (
        re.compile(
            r"(?i)\b(password|passwd|secret|token|api[_-]?key|authorization)"
            r"(['\"]?\s*[=:]\s*['\"]?)[^\s'\",&}]+"
        ),
        rf"\1\2{REDACTED}",
    ),
    # Email addresses (customer personal data)
    (re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), "[EMAIL]"),
    # Phone numbers in international format (+44 20 7946 0958, +1-555-010-0199). Only
    # with the "+": bare digit runs are durations, counts and IDs far more often.
    (re.compile(r"(?<![\w+])\+\d[\d ().-]{6,18}\d"), "[PHONE]"),
)

# Loggers that would otherwise be too chatty or leak full URLs (query strings can carry
# customer data) at INFO level. httpx2/httpcore2 are the HTTP stack of the OpenAI SDK and
# of Starlette's TestClient; "openai" is the SDK itself (request details at DEBUG).
_QUIET_LOGGERS = ("uvicorn.access", "httpx", "httpcore", "httpx2", "httpcore2", "openai")

_EVENT_PREFIX = re.compile(r"^event=([a-z0-9_]+)\b")


def safe_log_value(value: str, max_length: int = 200) -> str:
    """Make client-controlled text safe to embed in a log line.

    Non-printable characters are escaped (ESC -> "\\x1b"), so a request cannot use
    terminal escape sequences, backspaces or newlines to hide or rewrite log lines.
    (Starlette's ``request.url.path`` already drops CR/LF/TAB; other control
    characters such as ESC, NUL and BiDi overrides pass through.) Very long values
    are truncated.
    """
    escaped = "".join(
        char if char.isprintable() else char.encode("unicode_escape").decode("ascii")
        for char in value
    )
    return escaped if len(escaped) <= max_length else escaped[:max_length] + "...[truncated]"


def redact(text: str) -> str:
    """Mask credentials, tokens, email addresses and phone numbers in a piece of text."""
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


def valid_correlation_id(value: str | None) -> str | None:
    """The caller's correlation ID if it has the accepted shape, else None."""
    if value and CORRELATION_ID_PATTERN.fullmatch(value):
        return value
    return None


def set_error_category(scope: Any, category: str) -> None:
    """Record the error code of the response being built, for the access record."""
    scope[ERROR_CATEGORY_SCOPE_KEY] = category


class RequestContextFilter(logging.Filter):
    """Attach the service name and the current request and correlation IDs."""

    def __init__(self, service: str = DEFAULT_SERVICE) -> None:
        super().__init__()
        self.service = service

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_ctx.get()
        record.correlation_id = correlation_id_ctx.get()
        record.service = self.service
        return True


WITHHELD = "[message not logged]"


def safe_traceback(exc: BaseException) -> str:
    """A traceback with every frame but without exception messages (chained too)."""
    chain: list[BaseException] = []
    current: BaseException | None = exc
    while current is not None and all(current is not seen for seen in chain):
        chain.append(current)
        context = None if current.__suppress_context__ else current.__context__
        current = current.__cause__ or context
    parts = []
    for error in reversed(chain):
        frames = "".join(traceback.format_tb(error.__traceback__))
        name = f"{type(error).__module__}.{type(error).__qualname__}"
        parts.append(f"Traceback (most recent call last):\n{frames}{name}: {WITHHELD}")
    return "\n\nThe above exception led to:\n\n".join(parts)


class RedactingFormatter(logging.Formatter):
    """Text formatter that redacts the final line, including any traceback text.

    By default tracebacks are logged without exception messages (see the module
    docstring); ``exception_messages=True`` keeps them.
    """

    def __init__(
        self, fmt: str | None = None, *, exception_messages: bool = False, **kwargs: Any
    ) -> None:
        super().__init__(fmt, **kwargs)
        self.exception_messages = exception_messages

    def formatException(self, ei: Any) -> str:  # noqa: N802 - logging's method name
        exc = ei[1] if isinstance(ei, tuple) else None
        if self.exception_messages or not isinstance(exc, BaseException):
            return super().formatException(ei)
        return safe_traceback(exc)

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


class JsonFormatter(RedactingFormatter):
    """One JSON object per record, on one line, with fixed field names.

    Every string is redacted before it is encoded, so a pattern can never be split by
    JSON escaping. Field order is fixed (standard fields first) for readable output.
    """

    def format(self, record: logging.LogRecord) -> str:
        message = redact(record.getMessage())
        entry: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "level": record.levelname,
            "service": getattr(record, "service", DEFAULT_SERVICE),
            "logger": record.name,
            "request_id": _id_or_none(getattr(record, "request_id", None)),
            "correlation_id": getattr(record, "correlation_id", None),
            "event": None,
            "message": message,
        }
        fields = getattr(record, "log_fields", None)
        if isinstance(fields, dict):
            for key, value in fields.items():
                if key == "event" and entry["event"] is None:
                    entry["event"] = value
                elif key not in STANDARD_FIELDS:
                    entry[key] = redact(value) if isinstance(value, str) else value
        if entry["event"] is None and (prefix := _EVENT_PREFIX.match(message)):
            entry["event"] = prefix[1]
        if record.exc_info and record.exc_info[1] is not None:
            entry["error_type"] = type(record.exc_info[1]).__name__
            entry["exception"] = redact(self.formatException(record.exc_info))
        elif record.exc_text:
            entry["exception"] = redact(record.exc_text)
        return json.dumps(
            {key: value for key, value in entry.items() if value is not None},
            ensure_ascii=False,
            default=str,
        )


def _id_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and value != "-" else None


def configure_logging(
    level: str = "INFO",
    *,
    exception_messages: bool = False,
    log_format: Literal["text", "json"] = "text",
    service: str = DEFAULT_SERVICE,
) -> None:
    """Configure application-wide logging. Safe to call more than once.

    ``exception_messages`` keeps exception messages in tracebacks (development only).
    """
    formatter_class = JsonFormatter if log_format == "json" else RedactingFormatter
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "filters": {"context": {"()": RequestContextFilter, "service": service}},
            "formatters": {
                "default": {
                    "()": formatter_class,
                    "fmt": LOG_FORMAT,
                    "exception_messages": exception_messages,
                }
            },
            "handlers": {
                "console": {
                    "class": "logging.StreamHandler",
                    "stream": "ext://sys.stdout",
                    "formatter": "default",
                    "filters": ["context"],
                }
            },
            "root": {"level": level, "handlers": ["console"]},
            "loggers": {
                # Remove uvicorn's own handlers so its messages flow to the root
                # handler and share our format instead of being printed twice.
                "uvicorn": {"handlers": [], "propagate": True},
                "uvicorn.error": {"handlers": [], "propagate": True},
                # Our middleware already logs every request with timing and ID.
                **{
                    name: {"handlers": [], "level": "WARNING", "propagate": True}
                    for name in _QUIET_LOGGERS
                },
            },
        }
    )
