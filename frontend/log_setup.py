"""Logging for the Streamlit pages: the backend's fields, formats and redaction.

The pages do not import the backend (they run from their own image without ``app/``),
so this module mirrors ``app.core.logging_config`` for what the pages need. The field
names are the same, so one query finds a request on both sides (docs/logging.md);
``tests/test_logging.py`` checks that the two stay in step.

- Streamlit configures only its own ``streamlit`` logger, so without this the pages'
  INFO records were dropped and warnings were printed bare. Here one handler is put on
  the ``frontend`` logger (not the root, which Streamlit and libraries share) with
  ``propagate=False``. ``configure_frontend_logging`` runs on every script run and
  replaces its own handler, so it never duplicates output.
- Correlation: every page action (one Streamlit script run: a message sent, a button
  pressed) gets a correlation ID. Each backend call of that action, retries included,
  sends it as ``X-Correlation-ID``; the backend logs it next to its own request ID,
  and the page logs it with every record. The backend's ``X-Request-ID`` of each call
  is logged by the page as ``request_id``.
"""

import json
import logging
import re
import sys
import traceback
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any, Literal

CORRELATION_ID_HEADER = "X-Correlation-ID"
correlation_id_ctx: ContextVar[str | None] = ContextVar("frontend_correlation_id", default=None)

TEXT_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | cid=%(correlation_id)s | %(message)s"
REDACTED = "[REDACTED]"
WITHHELD = "[message not logged]"
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

# The backend's redactions (app.core.logging_config), kept identical by a test.
_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+"), f"Bearer {REDACTED}"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{8,}"), REDACTED),
    (re.compile(r"\b(?:cst|stf)_[A-Za-z0-9_-]{16,}"), REDACTED),
    (
        re.compile(
            r"(?i)\b(password|passwd|secret|token|api[_-]?key|authorization)"
            r"(['\"]?\s*[=:]\s*['\"]?)[^\s'\",&}]+"
        ),
        rf"\1\2{REDACTED}",
    ),
    (re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), "[EMAIL]"),
    (re.compile(r"(?<![\w+])\+\d[\d ().-]{6,18}\d"), "[PHONE]"),
)


def redact(text: str) -> str:
    """Mask credentials, tokens, email addresses and phone numbers."""
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


@contextmanager
def page_action() -> Iterator[str]:
    """A fresh correlation ID for one page action (one Streamlit script run)."""
    token = correlation_id_ctx.set(uuid.uuid4().hex)
    try:
        yield correlation_id_ctx.get() or ""
    finally:
        correlation_id_ctx.reset(token)


def correlation_headers() -> dict[str, str]:
    """The header that carries the current page action's ID to the backend, if any."""
    correlation_id = correlation_id_ctx.get()
    return {CORRELATION_ID_HEADER: correlation_id} if correlation_id else {}


def safe_traceback(exc: BaseException) -> str:
    """Every frame, but no exception message (it could quote what a customer typed)."""
    frames = "".join(traceback.format_tb(exc.__traceback__))
    name = f"{type(exc).__module__}.{type(exc).__qualname__}"
    return f"Traceback (most recent call last):\n{frames}{name}: {WITHHELD}"


class _ContextFilter(logging.Filter):
    def __init__(self, service: str) -> None:
        super().__init__()
        self.service = service

    def filter(self, record: logging.LogRecord) -> bool:
        record.service = self.service
        record.correlation_id = correlation_id_ctx.get() or "-"
        return True


class _TextFormatter(logging.Formatter):
    def formatException(self, ei: Any) -> str:  # noqa: N802 - logging's method name
        exc = ei[1] if isinstance(ei, tuple) else None
        return safe_traceback(exc) if isinstance(exc, BaseException) else ""

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        correlation_id = getattr(record, "correlation_id", "-")
        entry: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "level": record.levelname,
            "service": getattr(record, "service", "frontend"),
            "logger": record.name,
            "request_id": None,
            "correlation_id": None if correlation_id == "-" else correlation_id,
            "event": None,
            "message": redact(record.getMessage()),
        }
        fields = getattr(record, "log_fields", None)
        if isinstance(fields, dict):
            for key, value in fields.items():
                if key in ("event", "request_id") and entry[key] is None:
                    entry[key] = value
                elif key not in STANDARD_FIELDS:
                    entry[key] = redact(value) if isinstance(value, str) else value
        if record.exc_info and isinstance(record.exc_info[1], BaseException):
            entry["error_type"] = type(record.exc_info[1]).__name__
            entry["exception"] = redact(safe_traceback(record.exc_info[1]))
        return json.dumps(
            {key: value for key, value in entry.items() if value is not None},
            ensure_ascii=False,
            default=str,
        )


def configure_frontend_logging(
    service: str,
    level: str = "INFO",
    log_format: Literal["text", "json"] = "text",
) -> None:
    """Send the pages' records (``frontend.*``) to stdout, once, in the chosen format.

    Safe to call on every script run: the handler it added before is replaced.
    """
    logger = logging.getLogger("frontend")
    for handler in [h for h in logger.handlers if getattr(h, "_frontend_handler", False)]:
        logger.removeHandler(handler)
    handler = logging.StreamHandler(sys.stdout)
    handler._frontend_handler = True  # type: ignore[attr-defined]
    handler.addFilter(_ContextFilter(service))
    handler.setFormatter(_JsonFormatter() if log_format == "json" else _TextFormatter(TEXT_FORMAT))
    logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False


def configure_page_logging(service: str) -> None:
    """Configure logging from the page's settings (``LOG_LEVEL``, ``LOG_FORMAT``).

    With invalid settings it still configures INFO text logging, so the page can log
    the settings problem itself.
    """
    from pydantic import ValidationError

    from frontend.config import get_frontend_settings

    try:
        settings = get_frontend_settings()
    except ValidationError:
        configure_frontend_logging(service)
        return
    configure_frontend_logging(service, settings.log_level, settings.log_format)
