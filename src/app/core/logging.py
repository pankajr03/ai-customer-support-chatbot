"""Logging configuration with a per-request correlation ID."""

import logging
import logging.config
from contextvars import ContextVar

# Holds the ID of the request currently being handled. ContextVar keeps the
# value isolated between concurrent requests running on the same event loop.
request_id_ctx: ContextVar[str] = ContextVar("request_id", default="-")

LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | req=%(request_id)s | %(message)s"


class RequestIdFilter(logging.Filter):
    """Attach the current request ID to every log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_ctx.get()
        return True


def configure_logging(level: str = "INFO") -> None:
    """Configure the root logger to write structured lines to stdout."""
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "filters": {"request_id": {"()": RequestIdFilter}},
            "formatters": {"default": {"format": LOG_FORMAT}},
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
                # Our middleware already logs every request with timing and ID,
                # so silence uvicorn's duplicate access log.
                "uvicorn.access": {"level": "WARNING"},
            },
        }
    )
