"""Logging configuration built on Python's standard ``logging`` module.

Design:
- One handler, attached to the root logger only. Every other logger (ours,
  uvicorn's, third-party libraries') propagates up to it, so each record is
  written exactly once and in the same format.
- ``configure_logging`` is idempotent: ``dictConfig`` replaces existing root
  handlers, so calling it repeatedly (tests, reloads) never duplicates output.
- A per-request ID is attached to every record via a ContextVar.
- Output is passed through a redacting formatter as a last line of defence
  against secrets reaching the logs. The first line of defence is simply not
  logging sensitive values (see ``Settings.safe_summary`` and the request
  middleware, which logs the path but never the query string, headers or body).
- Tracebacks keep their frames (file, line, code) but not exception messages,
  which can quote customer text (a validation error's ``input_value=...``) or SQL
  parameters: each message is replaced by ``[message not logged]``. With
  ``DEBUG=true`` (never allowed in production) messages are kept.

Usage in any module:
    logger = logging.getLogger(__name__)
"""

import logging
import logging.config
import re
import traceback
from contextvars import ContextVar
from typing import Any

# Holds the ID of the request currently being handled. ContextVar keeps the
# value isolated between concurrent requests running on the same event loop.
request_id_ctx: ContextVar[str] = ContextVar("request_id", default="-")

LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | req=%(request_id)s | %(message)s"

REDACTED = "[REDACTED]"

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
)

# Loggers that would otherwise be too chatty or leak full URLs (query strings can carry
# customer data) at INFO level. httpx2/httpcore2 are the HTTP stack of the OpenAI SDK and
# of Starlette's TestClient; "openai" is the SDK itself (request details at DEBUG).
_QUIET_LOGGERS = ("uvicorn.access", "httpx", "httpcore", "httpx2", "httpcore2", "openai")


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
    """Mask credentials, tokens and email addresses in a piece of text."""
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


class RequestIdFilter(logging.Filter):
    """Attach the current request ID to every log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_ctx.get()
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
    """Formatter that redacts the final line, including any traceback text.

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


def configure_logging(level: str = "INFO", *, exception_messages: bool = False) -> None:
    """Configure application-wide logging. Safe to call more than once.

    ``exception_messages`` keeps exception messages in tracebacks (development only).
    """
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "filters": {"request_id": {"()": RequestIdFilter}},
            "formatters": {
                "default": {
                    "()": RedactingFormatter,
                    "fmt": LOG_FORMAT,
                    "exception_messages": exception_messages,
                }
            },
            "handlers": {
                "console": {
                    "class": "logging.StreamHandler",
                    "stream": "ext://sys.stdout",
                    "formatter": "default",
                    "filters": ["request_id"],
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
