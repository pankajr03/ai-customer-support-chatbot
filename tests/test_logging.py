"""Logging setup: levels, format, handler count and redaction."""

import logging
import re
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.logging_config import configure_logging, redact
from app.main import create_app

LINE_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3} \| (DEBUG|INFO|WARNING|ERROR|CRITICAL)\s* \| "
    r"[\w.]+ \| req=[\w-]+ \| .+$"
)


@pytest.fixture(autouse=True)
def reset_logging() -> Iterator[None]:
    """Leave logging in its default test state after each test."""
    yield
    configure_logging("INFO")


def test_repeated_configuration_does_not_duplicate_handlers(
    capsys: pytest.CaptureFixture[str],
) -> None:
    for _ in range(3):
        configure_logging("INFO")

    logging.getLogger("app.test").info("only once")

    assert len(logging.getLogger().handlers) == 1
    assert capsys.readouterr().out.count("only once") == 1


@pytest.mark.parametrize("level", ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"])
def test_configured_level_is_applied(level: str) -> None:
    configure_logging(level)

    assert logging.getLogger().level == getattr(logging, level)


def test_level_filters_lower_messages(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("WARNING")
    log = logging.getLogger("app.test")

    log.info("hidden info")
    log.warning("visible warning")

    out = capsys.readouterr().out
    assert "hidden info" not in out
    assert "visible warning" in out


def test_uvicorn_logs_share_the_app_format(capsys: pytest.CaptureFixture[str]) -> None:
    # Simulate uvicorn installing its own handlers before our app is imported.
    uvicorn_logger = logging.getLogger("uvicorn")
    uvicorn_logger.addHandler(logging.StreamHandler())
    uvicorn_logger.propagate = False

    configure_logging("INFO")
    logging.getLogger("uvicorn.error").info("server started")
    logging.getLogger("app.test").info("app message")

    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 2
    assert all(LINE_PATTERN.match(line) for line in lines), lines
    assert uvicorn_logger.handlers == []


def test_noisy_library_loggers_are_quiet(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("DEBUG")

    logging.getLogger("httpx").info("HTTP Request: GET https://api.example.com/?key=abc")
    logging.getLogger("uvicorn.access").info("127.0.0.1 - GET / 200")

    assert capsys.readouterr().out == ""


def test_request_log_omits_query_string(capsys: pytest.CaptureFixture[str]) -> None:
    client = TestClient(create_app(Settings(_env_file=None)))

    client.get("/api/v1/health?access_token=abc123&email=jane@example.com")

    out = capsys.readouterr().out
    assert "GET /api/v1/health -> 200" in out
    assert "abc123" not in out
    assert "jane@example.com" not in out


@pytest.mark.parametrize(
    ("message", "secret"),
    [
        ("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig", "eyJhbGciOiJIUzI1NiJ9"),
        ("calling LLM with key sk-proj-abcdefghijklmnop", "sk-proj-abcdefghijklmnop"),
        ("login password=hunter2 ok", "hunter2"),
        ("config {'api_key': 'abc123xyz'}", "abc123xyz"),
        ('payload {"token": "t0k3n-value"}', "t0k3n-value"),
        ("url /callback?secret=s3cr3t&x=1", "s3cr3t"),
        ("customer jane.doe+vip@example.co.uk asked for a refund", "jane.doe+vip@example.co.uk"),
    ],
)
def test_sensitive_values_are_redacted(message: str, secret: str) -> None:
    redacted = redact(message)

    assert secret not in redacted
    assert "[REDACTED]" in redacted or "[EMAIL]" in redacted


def test_safe_text_is_unchanged() -> None:
    message = "GET /api/v1/health -> 200 (1.2 ms) {'openai_api_key_set': True, 'debug': False}"

    assert redact(message) == message


def test_redaction_applies_to_real_log_output_and_tracebacks(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging("INFO")
    log = logging.getLogger("app.test")

    log.info("user %s sent token=%s", "jane@example.com", "abc123")
    try:
        raise ValueError("bad key sk-live-1234567890abcdef")
    except ValueError:
        log.exception("failure")

    out = capsys.readouterr().out
    assert "jane@example.com" not in out
    assert "abc123" not in out
    assert "sk-live-1234567890abcdef" not in out
    assert "Traceback" in out
