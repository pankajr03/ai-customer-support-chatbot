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

Usage in any module:
    logger = logging.getLogger(__name__)
"""

import logging
import logging.config
import re
from contextvars import ContextVar

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

# Loggers that would otherwise be too chatty or leak full URLs at INFO level.
_QUIET_LOGGERS = ("uvicorn.access", "httpx", "httpcore")


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


class RedactingFormatter(logging.Formatter):
    """Formatter that redacts the final line, including any traceback text."""

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def configure_logging(level: str = "INFO") -> None:
    """Configure application-wide logging. Safe to call more than once."""
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "filters": {"request_id": {"()": RequestIdFilter}},
            "formatters": {"default": {"()": RedactingFormatter, "fmt": LOG_FORMAT}},
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
