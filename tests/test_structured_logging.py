"""Structured logging (Stage 13): fields, JSON lines, correlation, levels and privacy.

The backend (``app.core.logging_config``) and the pages (``frontend.log_setup``) write the
same field names, so one query follows a page action into the backend. Field reference:
docs/logging.md.
"""

import json
import logging
import re
import uuid
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core import logging_config
from app.core.config import Settings
from app.core.logging_config import configure_logging, redact
from app.main import create_app
from app.services.faq_service import FAQService
from frontend import log_setup
from frontend.admin_api_client import AdminApiClient
from frontend.api_client import ApiError, ChatApiClient
from frontend.config import FrontendSettings

BASE = "/api/v1/conversations"
CORRELATION_ID = "3f2b8c1e7a4d4e9b9c612d5f0a8e4b17"
STANDARD = {"timestamp", "level", "service", "logger", "message"}
CUSTOMER_TEXT = "a customer wrote: my card is 4111 1111 1111 1111"
TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


@pytest.fixture(autouse=True)
def reset_logging() -> Iterator[None]:
    yield
    configure_logging("INFO")
    frontend_logger = logging.getLogger("frontend")
    for handler in list(frontend_logger.handlers):
        frontend_logger.removeHandler(handler)
    frontend_logger.propagate = True
    frontend_logger.setLevel(logging.NOTSET)


def json_records(out: str) -> list[dict[str, Any]]:
    """Every stdout line, parsed: each must be one complete JSON object."""
    records = [json.loads(line) for line in out.splitlines() if line.strip()]
    assert all(isinstance(record, dict) for record in records)
    return records


@pytest.fixture
def json_app(
    make_settings: Callable[..., Settings], faq_service: FAQService
) -> Callable[[], FastAPI]:
    """Builds the app in the test body: logging is configured then, so its handler writes
    to the stdout that capsys captures in that phase."""

    def build() -> FastAPI:
        settings = make_settings(log_format="json", service_name="support-api-test")
        return create_app(settings, faq_service=faq_service)

    return build


def access(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in records if r.get("event") == "http_request"]


# --- Backend: fields and format -----------------------------------------------------------


def test_every_json_record_is_one_line_with_the_standard_fields(
    json_app: Callable[[], FastAPI], capsys: pytest.CaptureFixture[str]
) -> None:
    with TestClient(json_app()) as client:
        response = client.get("/api/v1/faqs/categories")

    records = json_records(capsys.readouterr().out)
    assert records
    for record in records:
        assert STANDARD <= set(record), record
        assert TIMESTAMP.match(record["timestamp"])
        assert record["service"] == "support-api-test"
    (line,) = access(records)
    assert line == {
        "timestamp": line["timestamp"],
        "level": "INFO",
        "service": "support-api-test",
        "logger": "app.request",
        "request_id": response.headers["X-Request-ID"],
        "event": "http_request",
        "message": line["message"],
        "method": "GET",
        "route": "/api/v1/faqs/categories",
        "status": 200,
        "duration_ms": line["duration_ms"],
    }
    assert isinstance(line["duration_ms"], float)


def test_errors_carry_their_category_and_ids_stay_out_of_access_records(
    json_app: Callable[[], FastAPI], capsys: pytest.CaptureFixture[str]
) -> None:
    session_id = str(uuid.uuid4())
    with TestClient(json_app()) as client:
        client.get("/api/v1/faqs/faq_999")
        client.get(f"{BASE}/{session_id}/messages")  # no token: 401
        client.get("/api/v1/does-not-exist")

    records = access(json_records(capsys.readouterr().out))
    by_route = {(r["route"], r["status"]): r for r in records}
    assert by_route["/api/v1/faqs/{faq_id}", 404]["error_category"] == "NOT_FOUND"
    assert (
        by_route[f"{BASE}/{{session_id}}/messages", 401]["error_category"] == "UNAUTHORIZED_SESSION"
    )
    unmatched = by_route["unmatched", 404]
    assert unmatched["path"] == "/api/v1/does-not-exist"
    assert unmatched["error_category"] == "NOT_FOUND"
    text = json.dumps(records)
    assert session_id not in text and "faq_999" not in text


def test_a_rate_limited_request_is_categorised(
    make_settings: Callable[..., Settings],
    faq_service: FAQService,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = make_settings(log_format="json", rate_limit_requests_per_minute=1)
    with TestClient(create_app(settings, faq_service=faq_service)) as client:
        client.get("/api/v1/faqs/categories")
        assert client.get("/api/v1/faqs/categories").status_code == 429

    limited = [r for r in access(json_records(capsys.readouterr().out)) if r["status"] == 429]
    assert limited[0]["error_category"] == "RATE_LIMITED"


def test_levels_successful_health_checks_are_debug_and_server_errors_warning(
    json_app: Callable[[], FastAPI], capsys: pytest.CaptureFixture[str]
) -> None:
    app = json_app()

    @app.get("/api/v1/boom")
    def boom() -> None:
        raise RuntimeError(CUSTOMER_TEXT)  # frames keep source lines, so not inline

    with TestClient(app, raise_server_exceptions=False) as client:
        client.get("/api/v1/health")
        client.get("/api/v1/ready")
        client.get("/api/v1/boom")

    out = capsys.readouterr().out
    records = json_records(out)
    # INFO is the production level: Docker's 15-second health probes add nothing to it.
    assert [r["route"] for r in access(records)] == ["/api/v1/boom"]
    assert access(records)[0]["level"] == "WARNING"
    assert access(records)[0]["error_category"] == "INTERNAL_ERROR"
    (failure,) = [r for r in records if "exception" in r]
    assert failure["error_type"] == "RuntimeError"
    assert "Traceback" in failure["exception"]  # one record: the stack stays in its line
    assert "4111" not in out  # the exception message is withheld


def test_debug_level_shows_the_health_checks(
    make_settings: Callable[..., Settings],
    faq_service: FAQService,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = make_settings(log_format="json", log_level="DEBUG")
    with TestClient(create_app(settings, faq_service=faq_service)) as client:
        client.get("/api/v1/health")
    (line,) = access(json_records(capsys.readouterr().out))
    assert (line["level"], line["route"]) == ("DEBUG", "/api/v1/health")


def test_repeated_json_configuration_does_not_duplicate_handlers(
    capsys: pytest.CaptureFixture[str],
) -> None:
    for _ in range(3):
        configure_logging("INFO", log_format="json", service="svc")
    logging.getLogger("app.test").info("only once")

    assert len(logging.getLogger().handlers) == 1
    (record,) = json_records(capsys.readouterr().out)
    assert record["message"] == "only once"


def test_structured_fields_never_replace_the_standard_ones(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging("INFO", log_format="json", service="svc")
    logging.getLogger("app.test").info(
        "event=custom thing",
        extra={"log_fields": {"service": "forged", "level": "DEBUG", "count": 3}},
    )
    (record,) = json_records(capsys.readouterr().out)
    assert (record["service"], record["level"], record["count"]) == ("svc", "INFO", 3)
    assert record["event"] == "custom"  # taken from the message's event= prefix


# --- Backend: correlation -------------------------------------------------------------------


def test_a_valid_correlation_id_is_on_every_record_of_the_request(
    json_app: Callable[[], FastAPI], capsys: pytest.CaptureFixture[str]
) -> None:
    with TestClient(json_app()) as client:
        response = client.get("/api/v1/faqs/faq_999", headers={"X-Correlation-ID": CORRELATION_ID})
        client.get("/api/v1/faqs/categories")  # a later request: no correlation ID

    records = [r for r in json_records(capsys.readouterr().out) if "request_id" in r]
    mine = [r for r in records if r["request_id"] == response.headers["X-Request-ID"]]
    assert {r["event"] for r in mine} == {"api_error", "http_request"}
    assert all(r["correlation_id"] == CORRELATION_ID for r in mine)
    assert all("correlation_id" not in r for r in records if r not in mine)
    # Still the server's own request ID, never the caller's.
    assert response.headers["X-Request-ID"] != CORRELATION_ID
    assert "X-Correlation-ID" not in response.headers


@pytest.mark.parametrize(
    "value",
    ["short", "has spaces in it", "x" * 65, "-leading-dash-id", "id\x1b[2Kforged", "a/b/c/d/e/f"],
)
def test_a_malformed_correlation_id_is_ignored(
    json_app: Callable[[], FastAPI], capsys: pytest.CaptureFixture[str], value: str
) -> None:
    with TestClient(json_app()) as client:
        client.get("/api/v1/faqs/categories", headers={"X-Correlation-ID": value})
    (line,) = access(json_records(capsys.readouterr().out))
    assert "correlation_id" not in line


def test_browsers_may_send_the_correlation_header() -> None:
    from app.main import CORS_REQUEST_HEADERS

    assert "X-Correlation-ID" in CORS_REQUEST_HEADERS


# --- Backend: privacy, end to end -------------------------------------------------------------


def test_a_chat_turn_logs_no_message_contact_details_token_or_faq_text(
    json_app: Callable[[], FastAPI], faq_service: FAQService, capsys: pytest.CaptureFixture[str]
) -> None:
    message = (
        "Hi, I'm Jane Doe, jane.doe@example.com, +44 20 7946 0958. What is your return policy?"
    )
    with TestClient(json_app()) as client:
        created = client.post(BASE).json()
        sid, token = created["session_id"], created["access_token"]
        headers = {"Authorization": f"Bearer {token}", "X-Correlation-ID": CORRELATION_ID}
        reply = client.post(f"{BASE}/{sid}/chat", json={"message": message}, headers=headers)
        client.get(f"{BASE}/{sid}?access_token={token}")  # a token in the URL (refused)

    assert reply.status_code == 200
    out = capsys.readouterr().out
    records = json_records(out)
    assert any(r.get("event") == "conversation_turn" for r in records)
    for secret in (token, "jane.doe", "Jane Doe", "7946", "return policy", "access_token="):
        assert secret.lower() not in out.lower(), secret
    for faq in faq_service.list_active_faqs():  # approved answers are never logged
        assert faq.answer[:60] not in out


# --- Redaction ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        ("call me on +44 20 7946 0958", "7946 0958"),
        ("phone: +1-555-010-0199 please", "555-010-0199"),
        ("whatsapp +491701234567", "1701234567"),
    ],
)
def test_international_phone_numbers_are_redacted(text: str, secret: str) -> None:
    assert secret not in redact(text)
    assert "[PHONE]" in redact(text)


@pytest.mark.parametrize(
    "text",
    [
        "2026-10-19 12:00:00,123 | INFO",
        "duration_ms=1234567.8",
        "revision=+3 faqs=30",
        "tz +00:00",
        "ticket 3f2b8c1e-7a4d-4e9b-9c61-2d5f0a8e4b17",
    ],
)
def test_phone_redaction_leaves_timestamps_ids_and_numbers_alone(text: str) -> None:
    assert redact(text) == text


def test_redaction_keeps_json_records_valid(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO", log_format="json")
    logging.getLogger("app.test").warning('token="cst_%s" sent by "jane@example.com"', "a" * 43)
    (record,) = json_records(capsys.readouterr().out)
    assert "cst_" not in record["message"] and "jane@" not in record["message"]


# --- Frontend --------------------------------------------------------------------------------


def test_frontend_redaction_matches_the_backend() -> None:
    def patterns(module: Any) -> list[tuple[str, int, str]]:
        return [(p.pattern, p.flags, r) for p, r in module._REDACTIONS]

    assert patterns(log_setup) == patterns(logging_config)
    assert log_setup.STANDARD_FIELDS == logging_config.STANDARD_FIELDS


def test_frontend_records_reach_stdout_once_in_json(capsys: pytest.CaptureFixture[str]) -> None:
    """Before Stage 13 nothing configured the pages' loggers under Streamlit: INFO records
    were dropped and warnings were printed bare."""
    for _ in range(3):  # every Streamlit script run configures again
        log_setup.configure_frontend_logging("customer-frontend", "INFO", "json")
    with log_setup.page_action() as correlation_id:
        logging.getLogger("frontend.app").info("hello")

    assert len(logging.getLogger("frontend").handlers) == 1
    (record,) = json_records(capsys.readouterr().out)
    assert record["service"] == "customer-frontend"
    assert record["correlation_id"] == correlation_id
    assert TIMESTAMP.match(record["timestamp"])


def test_each_page_action_has_its_own_correlation_id() -> None:
    assert log_setup.correlation_headers() == {}
    with log_setup.page_action() as first:
        assert log_setup.correlation_headers() == {"X-Correlation-ID": first}
    with log_setup.page_action() as second:
        pass
    assert first != second
    assert logging_config.valid_correlation_id(first) == first  # the backend accepts it
    assert log_setup.correlation_headers() == {}


def _recording_client(*responses: httpx.Response) -> tuple[httpx.Client, list[httpx.Request]]:
    sent: list[httpx.Request] = []
    queue = list(responses)

    def handle(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return queue.pop(0)

    transport = httpx.MockTransport(handle)
    return httpx.Client(base_url="http://backend.test", transport=transport), sent


def test_the_chat_client_sends_the_correlation_id_on_every_attempt_and_logs_both_ids(
    capsys: pytest.CaptureFixture[str],
) -> None:
    log_setup.configure_frontend_logging("customer-frontend", "INFO", "json")
    busy = httpx.Response(
        503,
        json={"error": {"code": "SERVICE_UNAVAILABLE", "message": "x", "request_id": "r1"}},
        headers={"X-Request-ID": "r1", "Retry-After": "0"},
    )
    http, sent = _recording_client(busy, busy, busy)
    client = ChatApiClient(http_client=http, max_retries=2, sleep=lambda seconds: None)

    with log_setup.page_action() as correlation_id, pytest.raises(ApiError):
        client.create_conversation()

    assert sent and all(r.headers["X-Correlation-ID"] == correlation_id for r in sent)
    records = [r for r in json_records(capsys.readouterr().out) if r.get("event")]
    call = records[0]
    assert call["event"] == "backend_call"
    assert (call["request_id"], call["correlation_id"]) == ("r1", correlation_id)
    assert (call["route"], call["status"]) == ("create", 503)
    assert call["error_category"] == "SERVICE_UNAVAILABLE"


def test_an_unreachable_backend_is_logged_with_its_category(
    capsys: pytest.CaptureFixture[str],
) -> None:
    log_setup.configure_frontend_logging("customer-frontend", "INFO", "json")

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    http = httpx.Client(base_url="http://backend.test", transport=httpx.MockTransport(refuse))
    with pytest.raises(ApiError):
        ChatApiClient(http_client=http).check_health()
    (record,) = json_records(capsys.readouterr().out)
    assert (record["level"], record["error_category"]) == ("WARNING", "backend_unreachable")


def test_the_admin_client_sends_the_correlation_id_and_never_logs_the_token(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from pydantic import SecretStr

    log_setup.configure_frontend_logging("admin-frontend", "INFO", "json")
    staff_token = "stf_" + "s" * 43
    identity = {"staff_id": "alice", "role": "admin", "permissions": []}
    http, sent = _recording_client(
        httpx.Response(200, json=identity, headers={"X-Request-ID": "r9"})
    )
    with log_setup.page_action() as correlation_id, pytest.raises(Exception):  # noqa: B017
        # The identity shape may differ; the call and its log line are what count here.
        AdminApiClient(http_client=http).who_am_i(SecretStr(staff_token))

    assert sent[0].headers["X-Correlation-ID"] == correlation_id
    out = capsys.readouterr().out
    assert staff_token not in out
    call = next(r for r in json_records(out) if r.get("event") == "backend_call")
    assert (call["request_id"], call["service"]) == ("r9", "admin-frontend")


def test_frontend_settings_read_the_log_variables() -> None:
    settings = FrontendSettings(_env_file=None, log_level=" debug ", log_format="JSON")
    assert (settings.log_level, settings.log_format) == ("DEBUG", "json")
    assert FrontendSettings(_env_file=None).log_format == "text"
