"""Error responses and diagnostic logging for the FAQ feature (Task 8 cases)."""

import json
from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.dependencies import get_faq_service
from app.api.errors import FAQ_UNAVAILABLE_MESSAGE
from app.core.config import Settings
from app.core.logging_config import safe_log_value
from app.database.faq_repository import (
    DuplicateFAQIdError,
    FAQDataError,
    FAQFileNotFoundError,
    InvalidFAQRecordError,
    MalformedFAQFileError,
)
from app.main import create_app
from app.models.faq import FAQ_EXAMPLE

FAQS_URL = "/api/v1/faqs"
CLIENT_DATA_DIR = "C:/srv/acme-client-private/data"


@pytest.fixture
def logged_client(
    make_settings: Callable[..., Settings], caplog: pytest.LogCaptureFixture
) -> tuple[FastAPI, TestClient, Callable[[], str]]:
    """A client plus a function returning (and clearing) the log text captured so far."""
    app = create_app(make_settings())
    caplog.clear()  # discard startup lines

    def read_log() -> str:
        text = caplog.text
        caplog.clear()
        return text

    return app, TestClient(app), read_log


def assert_no_internals(text: str) -> None:
    """Client-facing text must never reveal paths, exception types or tracebacks."""
    for marker in ("Traceback", "Error:", "faqs.json", "\\", "C:/", CLIENT_DATA_DIR, ".py"):
        assert marker not in text, f"{marker!r} leaked in: {text}"


# --- Client errors ------------------------------------------------------------------


def test_missing_faq_id_is_404(logged_client) -> None:
    _, client, read_log = logged_client

    response = client.get(f"{FAQS_URL}/faq_999")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"
    assert response.json()["error"]["message"] == "FAQ not found."
    assert "GET /api/v1/faqs/faq_999 -> 404" in read_log()


@pytest.mark.parametrize("query", ["", "   "])
def test_empty_search_query_is_422_and_logged_without_value(logged_client, query: str) -> None:
    _, client, read_log = logged_client

    response = client.get(f"{FAQS_URL}/search", params={"q": query})

    assert response.status_code == 422
    assert response.json()["error"]["details"][0]["loc"] == ["query", "q"]
    assert "Rejected GET /api/v1/faqs/search: query.q (string_too_short)" in read_log()


def test_missing_search_query_is_422(logged_client) -> None:
    _, client, read_log = logged_client

    assert client.get(f"{FAQS_URL}/search").status_code == 422
    assert "query.q (missing)" in read_log()


def test_unknown_category_is_422_and_lists_valid_values(logged_client) -> None:
    _, client, read_log = logged_client

    response = client.get(FAQS_URL, params={"category": "billing"})

    assert response.status_code == 422
    assert "'shipping'" in response.json()["error"]["details"][0]["msg"]
    assert "Rejected GET /api/v1/faqs: query.category (enum)" in read_log()


def test_rejected_values_are_not_logged(logged_client) -> None:
    _, client, read_log = logged_client
    personal = "jane.doe@example.com my card 4111 1111 1111 1111 " + "x" * 200

    client.get(f"{FAQS_URL}/search", params={"q": personal})
    client.get(FAQS_URL, params={"category": "jane.doe@example.com"})

    log = read_log()
    assert "query.q (string_too_long)" in log
    assert "jane.doe" not in log
    assert "4111" not in log


@pytest.mark.parametrize("path", ["/api/v1/answer", "/api/v1/hybrid-search"])
def test_invalid_unicode_in_a_json_body_is_422_not_500(logged_client, path: str) -> None:
    """An unpaired surrogate escape is valid JSON but not text; echoing it back in the
    validation error used to make rendering the response fail."""
    _, client, _ = logged_client
    body = b'{"query": "a \\ud800 b"}'

    response = client.post(path, content=body, headers={"Content-Type": "application/json"})

    assert response.status_code == 422
    assert response.json()["error"]["details"][0]["type"] == "string_unicode"


# --- Data-loading failures at startup -------------------------------------------------


def _write(tmp_path: Path, name: str, content: str | None) -> Path:
    path = tmp_path / name
    if content is not None:
        path.write_text(content, encoding="utf-8")
    return path


STARTUP_FAILURES = [
    ("missing.json", None, FAQFileNotFoundError),
    ("malformed.json", "[{", MalformedFAQFileError),
    ("invalid.json", json.dumps([{**FAQ_EXAMPLE, "answer": "   "}]), InvalidFAQRecordError),
    ("duplicate.json", json.dumps([FAQ_EXAMPLE, FAQ_EXAMPLE]), DuplicateFAQIdError),
]


@pytest.mark.parametrize(("name", "content", "error"), STARTUP_FAILURES)
def test_data_loading_failure_stops_startup_with_clear_log(
    make_settings: Callable[..., Settings],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    name: str,
    content: str | None,
    error: type[FAQDataError],
) -> None:
    path = _write(tmp_path, name, content)

    with pytest.raises(error):
        create_app(make_settings(faq_data_path=path))

    log = capsys.readouterr().out
    assert "| CRITICAL | app.main |" in log
    assert f"Cannot start: FAQ knowledge base failed to load ({error.__name__})" in log
    assert name in log  # operators need to know which file to fix


def test_startup_log_for_invalid_records_omits_record_contents(
    make_settings: Callable[..., Settings], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    secret_answer = "INTERNAL: refund code ABC-123 for VIP customers only " + "x" * 6000
    path = _write(tmp_path, "faqs.json", json.dumps([{**FAQ_EXAMPLE, "answer": secret_answer}]))

    with pytest.raises(InvalidFAQRecordError):
        create_app(make_settings(faq_data_path=path))

    log = capsys.readouterr().out
    assert "record 0 (id='faq_001') answer" in log
    assert "ABC-123" not in log


# --- Data-loading failures during a request -------------------------------------------


class BrokenFAQService:
    """Simulates the knowledge base becoming unavailable after startup."""

    def __init__(self, error: Exception) -> None:
        self.error = error

    def __getattr__(self, name: str) -> Callable[..., object]:
        def fail(*args: object, **kwargs: object) -> object:
            raise self.error

        return fail


@pytest.mark.parametrize(
    "error",
    [
        FAQFileNotFoundError("FAQ file not found", Path(CLIENT_DATA_DIR) / "faqs.json"),
        MalformedFAQFileError(
            "FAQ file is not valid JSON", Path(CLIENT_DATA_DIR) / "faqs.json", 3, 4
        ),
        DuplicateFAQIdError(Path(CLIENT_DATA_DIR) / "faqs.json", ["faq_001"]),
    ],
)
@pytest.mark.parametrize("path", ["", "/categories", "/search?q=track", "/faq_001"])
def test_data_failure_during_request_is_503_without_internals(
    logged_client, error: FAQDataError, path: str
) -> None:
    app, client, read_log = logged_client
    app.dependency_overrides[get_faq_service] = lambda: BrokenFAQService(error)

    response = client.get(f"{FAQS_URL}{path}")

    assert response.status_code == 503
    body = response.json()
    assert body == {
        "error": {
            "code": "SERVICE_UNAVAILABLE",
            "message": FAQ_UNAVAILABLE_MESSAGE,
            "request_id": response.headers["X-Request-ID"],
        }
    }
    assert_no_internals(response.text)

    log = read_log()
    assert (
        f"FAQ data unavailable on GET /api/v1/faqs{path.split('?')[0]}: {type(error).__name__}"
        in log
    )
    assert "acme-client-private" in log  # full diagnostics stay server-side
    assert "Traceback" not in log  # known failure: a descriptive line, not a stack dump


# --- Unexpected exceptions are never hidden -------------------------------------------


@pytest.mark.parametrize("error", [ValueError("boom"), KeyError("faq_001"), RuntimeError("x")])
def test_unexpected_errors_are_500_not_success(logged_client, error: Exception) -> None:
    app, client, read_log = logged_client
    app.dependency_overrides[get_faq_service] = lambda: BrokenFAQService(error)

    response = client.get(FAQS_URL)

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INTERNAL_ERROR"
    assert_no_internals(response.text)
    log = read_log()
    assert "Unhandled error on GET /api/v1/faqs" in log
    assert "Traceback" in log  # unexpected: keep the full stack trace for debugging


# --- Log injection --------------------------------------------------------------------


def test_control_characters_in_path_are_escaped_in_logs(logged_client) -> None:
    """ESC[2K (erase line) + ESC[1A (cursor up) could hide earlier log lines in a terminal."""
    _, client, read_log = logged_client

    client.get(f"{FAQS_URL}/faq_1%1B[2K%1B[1A%08%E2%80%AEFORGED")

    log = read_log()
    assert not any(char in log for char in ("\x1b", "\x08", "‮"))
    assert "faq_1\\x1b[2K\\x1b[1A\\x08\\u202eFORGED" in log


def test_newlines_in_path_never_reach_the_log(logged_client) -> None:
    _, client, read_log = logged_client
    forged = "2026-01-01 00:00:00,000 | CRITICAL | app.main | req=- | System compromised"

    client.get(f"{FAQS_URL}/faq_1%0A{forged}")

    assert not any(line.startswith("2026-01-01") for line in read_log().splitlines())


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("/api/v1/faqs", "/api/v1/faqs"),
        ("a\nb", "a\\nb"),
        ("a\r\nb\tc", "a\\r\\nb\\tc"),
        ("bell\x07", "bell\\x07"),
        ("café", "café"),
        ("x" * 250, "x" * 200 + "...[truncated]"),
    ],
)
def test_safe_log_value(value: str, expected: str) -> None:
    assert safe_log_value(value) == expected
