"""Stage 13 (Task 16): safe error reporting in production mode.

Each failure an operator can meet in production, run against an app built with
``APP_ENV=production``:

| Scenario                       | Customer / caller gets                    | Recoverable |
|--------------------------------|-------------------------------------------|-------------|
| Backend unavailable            | page: BACKEND_UNREACHABLE, retry offered  | yes         |
| OpenAI timeout                 | 200, the approved FAQ answer (fallback)   | n/a         |
| Embedding model unavailable    | chat 200 (keyword); search 503            | yes         |
| Database locked                | 503 SERVICE_UNAVAILABLE + Retry-After     | yes         |
| Invalid environment config     | process stops: variable names, no values  | no          |
| Missing API key                | stops if LLM_ENABLED; else FAQ answers    | no          |
| Invalid session                | 404 SESSION_NOT_FOUND                     | no          |
| Staff authorization failure    | 401 / 403, audited                        | no          |
| FAQ index-refresh failure      | edit saved; /ready says unavailable       | n/a         |
| Support ticket creation fails  | 503, same client_request_id is safe       | yes         |

Every error body carries the request ID (as in ``X-Request-ID``) and ``retryable``;
none carries a stack trace, exception type, file path, SQL or configuration value. The
diagnostic detail goes to the server log instead, at a level an operator will see.
"""

import json
import logging
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import openai
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError
from sqlalchemy.exc import OperationalError

from app.core.config import Settings, describe_settings_error
from app.database.connection import Database
from app.main import create_app
from app.models.errors import RETRYABLE_CODES, ErrorCode, ErrorResponse, is_retryable
from app.repositories.ticket_repository import _store_errors
from app.services.embedding_service import EmbeddingService
from app.services.llm_service import LLMService
from app.services.semantic_search_service import SemanticSearchService
from frontend.api_client import ApiError, ChatApiClient, ErrorKind
from tests.fakes import FAKE_MODEL_NAME, FAQS
from tests.test_admin_api import ALEX, MARIA, NEW_FAQ, STAFF_HASHES, bearer
from tests.test_admin_api import FAQS as ADMIN_FAQS
from tests.test_api_workflows import ScriptedEncoder
from tests.test_customer_chat_service import FakeClient
from tests.test_frontend_api_client import raise_, scripted

CONVERSATIONS = "/api/v1/conversations"
TICKETS = "/api/v1/support/tickets"
READY = "/api/v1/ready"
RETURN_POLICY = "What is your return policy?"
KEY = SecretStr("sk-test-SECRET-0123456789abcdef")
LOCKED = OperationalError("UPDATE support_tickets", {}, Exception("database is locked"))
DISK = OperationalError("INSERT INTO x VALUES (?)", {}, Exception("disk I/O error"))
# Never in a public response: internals, and anything from the environment.
LEAKS = (
    "Traceback",
    'File "',
    ".py",
    "OperationalError",
    "Exception",
    "sqlite",
    "SELECT",
    "INSERT",
    "UPDATE",
    "locked",
    "disk I/O",
    "sk-test",
    "OPENAI",
    "DATABASE_URL",
    FAKE_MODEL_NAME,
)


class SwitchableEncoder(ScriptedEncoder):
    """The workflow tests' encoder, which can be made to fail (a broken model)."""

    def __init__(self) -> None:
        super().__init__()
        self.broken = False

    def encode(self, sentences: list[str], **kwargs: Any) -> Any:
        if self.broken:
            raise RuntimeError("CUDA error: device-side assert at C:/models/encoder.py")
        return super().encode(sentences, **kwargs)


class Production:
    def __init__(self, client: TestClient, app: FastAPI, encoder: SwitchableEncoder) -> None:
        self.client, self.app, self.encoder = client, app, encoder

    def conversation(self) -> tuple[str, dict[str, str]]:
        body = self.client.post(CONVERSATIONS).json()
        return body["session_id"], bearer(body["access_token"])

    def chat(self, message: str = RETURN_POLICY) -> httpx.Response:
        sid, headers = self.conversation()
        return self.client.post(
            f"{CONVERSATIONS}/{sid}/chat", json={"message": message}, headers=headers
        )


@pytest.fixture
def make_production(
    make_settings: Callable[..., Settings], faq_file: Path, tmp_path: Path
) -> Iterator[Callable[..., Production]]:
    """A running app with APP_ENV=production, a database file and staff accounts."""
    clients: list[TestClient] = []

    def _make(*, loader: Any = None, llm: LLMService | None = None) -> Production:
        path = (tmp_path / f"production-{len(clients)}.db").as_posix()
        settings = make_settings(
            app_env="production",
            database_url=f"sqlite:///{path}",
            faq_data_path=faq_file,
            staff_token_hashes=STAFF_HASHES,
            query_rewrite_use_llm=False,
        )
        assert not settings.debug and not settings.docs_enabled
        holder: list[FastAPI] = []
        encoder = SwitchableEncoder()
        semantic = SemanticSearchService(
            lambda: holder[0].state.faq_service.list_active_faqs(),
            EmbeddingService(FAKE_MODEL_NAME, loader=loader or (lambda model: encoder)),
            catalog_revision=lambda: holder[0].state.faq_service.catalog_revision(),
        )
        app = create_app(
            settings, semantic_search_service=semantic, database=Database(f"sqlite:///{path}"),
            llm_service=llm,
        )  # fmt: skip
        holder.append(app)
        client = TestClient(app, raise_server_exceptions=False)
        client.__enter__()
        clients.append(client)
        return Production(client, app, encoder)

    yield _make
    for client in clients:
        client.__exit__(None, None, None)


@pytest.fixture
def production(make_production: Callable[..., Production]) -> Production:
    return make_production()


def safe_error(response: httpx.Response, status: int, code: str) -> dict[str, Any]:
    """Check a public error response and return its ``error`` object."""
    assert response.status_code == status, response.text
    body = response.json()
    ErrorResponse.model_validate(body)
    error = body["error"]
    assert error["code"] == code
    assert error["request_id"] == response.headers["X-Request-ID"]  # preserved
    assert error["retryable"] is is_retryable(ErrorCode(code))
    assert_nothing_internal(response)
    return dict(error)


def assert_nothing_internal(response: httpx.Response) -> None:
    for leak in LEAKS:
        assert leak not in response.text, leak


def capture_logs(caplog: pytest.LogCaptureFixture) -> None:
    """Re-attach pytest's log capture after an app was built inside the test body
    (``create_app`` reconfigures the root logger's handlers)."""
    logging.getLogger().addHandler(caplog.handler)


def counter(production: Production, name: str, **labels: str) -> int:
    """A process counter, as the admin monitoring endpoint reports it."""
    response = production.client.get("/api/v1/admin/monitoring", headers=bearer(ALEX))
    assert response.status_code == 200, response.text
    rows = response.json()["counters"].get(name, [])
    return sum(row["count"] for row in rows if labels.items() <= row["labels"].items())


def api_errors(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.getMessage().startswith("event=api_error")]


# --- The contract ----------------------------------------------------------------------


def test_only_temporary_conditions_are_retryable() -> None:
    assert RETRYABLE_CODES == {
        ErrorCode.SERVICE_UNAVAILABLE,
        ErrorCode.RATE_LIMITED,
        ErrorCode.REQUEST_IN_PROGRESS,
    }
    # An unexpected fault is reported, not retried in a loop.
    assert not is_retryable(ErrorCode.INTERNAL_ERROR)
    assert not is_retryable(ErrorCode.SESSION_NOT_FOUND)


def test_an_unexpected_error_is_a_generic_500_with_the_traceback_kept_server_side(
    production: Production, caplog: pytest.LogCaptureFixture
) -> None:
    @production.app.get("/api/v1/test-crash")
    def crash() -> None:
        raise RuntimeError(f"failed at C:/srv/app.py with {KEY.get_secret_value()}")

    response = production.client.get("/api/v1/test-crash")

    error = safe_error(response, 500, "INTERNAL_ERROR")
    assert error["retryable"] is False
    assert response.headers["Cache-Control"] == "no-store"
    unhandled = [r for r in caplog.records if r.getMessage().startswith("Unhandled error")]
    assert unhandled and unhandled[0].levelno == logging.ERROR
    assert unhandled[0].exc_info is not None  # the traceback, for us only
    assert any(r.request_id == error["request_id"] for r in caplog.records  # type: ignore[attr-defined]
               if r.getMessage().startswith("event=api_error status=500"))  # fmt: skip


def test_the_error_log_line_says_whether_the_failure_is_recoverable(
    production: Production, caplog: pytest.LogCaptureFixture
) -> None:
    sid, headers = production.conversation()
    production.client.post(f"{CONVERSATIONS}/{sid}/close", headers=headers)
    production.client.post(f"{CONVERSATIONS}/{sid}/chat", json={"message": "Hi"}, headers=headers)

    (record,) = api_errors(caplog)
    fields = record.log_fields  # type: ignore[attr-defined]
    assert fields["error_category"] == "SESSION_CLOSED" and fields["retryable"] is False


# --- 1. Backend unavailable ------------------------------------------------------------


def test_backend_unavailable_is_a_retryable_connection_error_on_the_page(
    caplog: pytest.LogCaptureFixture,
) -> None:
    http, requests = scripted(raise_(httpx.ConnectError("[Errno 111] Connection refused")))

    with pytest.raises(ApiError) as caught:
        ChatApiClient(http_client=http, max_retries=0).create_conversation()

    assert caught.value.kind is ErrorKind.BACKEND_UNREACHABLE and caught.value.retryable
    assert "Connection refused" not in str(caught.value)  # what the page may show or log
    failure = next(r for r in caplog.records if "backend unreachable" in r.getMessage())
    assert failure.levelno == logging.WARNING  # never silent
    assert failure.log_fields["error_category"] == "backend_unreachable"  # type: ignore[attr-defined]


# --- 2. OpenAI timeout -----------------------------------------------------------------


def test_an_openai_timeout_falls_back_to_the_approved_answer_and_is_logged(
    make_production: Callable[..., Production], caplog: pytest.LogCaptureFixture
) -> None:
    timeout = openai.APITimeoutError(request=httpx.Request("POST", "https://api.openai.com"))
    client = FakeClient(timeout)
    llm = LLMService(KEY, model="gpt-5.4-mini", max_retries=0, client_factory=lambda key: client)  # type: ignore[arg-type,return-value]
    production = make_production(llm=llm)
    capture_logs(caplog)

    response = production.chat()

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["generation_method"] == "approved_faq"
    assert body["answer"] == FAQS["faq_106"].answer
    assert len(client.calls) == 1
    assert_nothing_internal(response)
    gave_up = [r for r in caplog.records if "giving up" in r.getMessage()]
    assert gave_up and gave_up[0].levelno == logging.ERROR
    assert llm.error_counts() == {"timeout": 1}  # counted for the monitoring dashboard


# --- 3. Embedding model unavailable ----------------------------------------------------


def _missing_model(name: str) -> Any:
    raise OSError(f"{name} not found in C:/Users/ops/.cache/huggingface")


def test_an_unavailable_embedding_model_degrades_chat_and_refuses_search_safely(
    make_production: Callable[..., Production], caplog: pytest.LogCaptureFixture
) -> None:
    production = make_production(loader=_missing_model)
    capture_logs(caplog)

    chat = production.chat()
    assert chat.status_code == 200, chat.text  # keyword retrieval or the safe message
    assert_nothing_internal(chat)

    search = production.client.post("/api/v1/semantic-search", json={"query": "returns"})
    error = safe_error(search, 503, "SERVICE_UNAVAILABLE")
    assert error["retryable"] is True
    ready = production.client.get(READY).json()
    assert ready["status"] == "ready" and ready["checks"]["semantic_search"] == "unavailable"
    load = [r for r in caplog.records if r.getMessage().startswith("Failed to load embedding")]
    assert load and load[0].levelno == logging.ERROR
    assert "not found in C:/Users/ops" in load[0].getMessage()  # the cause, server-side


# --- 4. Database locked ----------------------------------------------------------------


def raising(error: Exception, operation: str) -> Callable[..., Any]:
    """A repository method that fails the way the repository does: a typed store error
    (busy for a locked database) raised from the driver's error."""

    def broken(*args: Any, **kwargs: Any) -> Any:
        with _store_errors(operation):
            raise error

    return broken


def failing(error: Exception) -> Callable[..., Any]:
    @contextmanager
    def broken(*args: Any, **kwargs: Any) -> Iterator[Any]:
        raise error
        yield  # pragma: no cover

    return broken


def ticket_body(session_id: str, request_id: uuid.UUID) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "client_request_id": str(request_id),
        "ticket_type": "support",
        "subject": "Refund assistance",
        "description": "I need help with a refund request.",
        "contact": {
            "name": "Example Customer",
            "email": "customer@example.com",
            "phone": None,
            "contact_preference": "email",
            "consent_given": True,
        },
        "confirmed": True,
    }


def test_a_locked_database_is_a_retryable_503_with_retry_after(
    production: Production, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    sid, headers = production.conversation()
    monkeypatch.setattr(
        production.app.state.ticket_repository, "create_ticket", raising(LOCKED, "create")
    )

    response = production.client.post(TICKETS, json=ticket_body(sid, uuid.uuid4()), headers=headers)

    error = safe_error(response, 503, "SERVICE_UNAVAILABLE")
    assert error["retryable"] is True
    assert response.headers["Retry-After"] == "1"
    assert response.headers["Cache-Control"] == "no-store"
    # The log names what actually failed (by type), never the SQL or its parameters.
    assert "Support storage unavailable on POST /api/v1/support/tickets: TicketStoreBusyError" \
        in caplog.text  # fmt: skip
    (record,) = api_errors(caplog)
    assert record.log_fields["cause"] == "TicketStoreBusyError"  # type: ignore[attr-defined]
    assert "support_tickets" not in caplog.text and "customer@example.com" not in caplog.text


def test_locked_conversation_storage_is_a_retryable_503_with_retry_after(
    production: Production, monkeypatch: pytest.MonkeyPatch
) -> None:
    sid, headers = production.conversation()
    monkeypatch.setattr(production.app.state.database, "write", failing(LOCKED))

    response = production.client.post(
        f"{CONVERSATIONS}/{sid}/chat", json={"message": RETURN_POLICY}, headers=headers
    )

    safe_error(response, 503, "SERVICE_UNAVAILABLE")
    assert response.headers["Retry-After"] == "1"


# --- 5. Invalid environment configuration -----------------------------------------------


def test_invalid_configuration_names_the_variables_but_never_their_values() -> None:
    secret = "sk-proj-hunter2 0123456789abcdef"  # noqa: S105 - a fake key
    with pytest.raises(ValidationError) as caught:
        Settings(  # type: ignore[call-arg]
            _env_file=None, app_env="production", debug=True, llm_enabled=True,
            openai_api_key=secret,
        )  # fmt: skip

    message = describe_settings_error(caught.value)
    assert message.startswith("Cannot start:") and "OPENAI_API_KEY" in message
    assert "hunter2" not in message and "0123456789" not in message
    assert "Traceback" not in message


# --- 6. Missing API key ----------------------------------------------------------------


def test_llm_enabled_without_an_api_key_refuses_to_start() -> None:
    with pytest.raises(ValidationError) as caught:
        Settings(_env_file=None, llm_enabled=True)  # type: ignore[call-arg]

    assert "LLM_ENABLED=true requires OPENAI_API_KEY" in describe_settings_error(caught.value)


def test_without_an_api_key_customers_get_approved_answers_and_readiness_says_so(
    production: Production,
) -> None:
    response = production.chat()

    assert response.status_code == 200
    assert response.json()["generation_method"] == "approved_faq"
    assert production.client.get(READY).json()["checks"]["llm"] == "disabled"


# --- 7. Invalid session ----------------------------------------------------------------


def test_an_invalid_session_is_a_non_retryable_404_that_reveals_nothing(
    production: Production,
) -> None:
    sid, headers = production.conversation()
    wrong = bearer("cst_" + "x" * 43)
    unknown = f"{CONVERSATIONS}/{uuid.uuid4()}/chat"

    a = safe_error(
        production.client.post(f"{CONVERSATIONS}/{sid}/chat", json={"message": "Hi"},
                               headers=wrong), 404, "SESSION_NOT_FOUND")  # fmt: skip
    b = safe_error(
        production.client.post(unknown, json={"message": "Hi"}, headers=headers),
        404,
        "SESSION_NOT_FOUND",
    )
    assert a["retryable"] is False
    assert {k: v for k, v in a.items() if k != "request_id"} == {
        k: v for k, v in b.items() if k != "request_id"
    }
    assert a["request_id"] != b["request_id"]


# --- 8. Staff authorization failure ----------------------------------------------------


def test_staff_authorization_failures_are_refused_audited_and_not_retryable(
    production: Production, caplog: pytest.LogCaptureFixture
) -> None:
    missing = production.client.get(ADMIN_FAQS)
    forged = production.client.get(ADMIN_FAQS, headers=bearer("stf_" + "y" * 43))
    support_only = production.client.get(ADMIN_FAQS, headers=bearer(MARIA))

    safe_error(missing, 401, "STAFF_AUTH_REQUIRED")
    safe_error(forged, 401, "STAFF_AUTH_REQUIRED")
    error = safe_error(support_only, 403, "STAFF_FORBIDDEN")
    assert "manage_faqs" not in support_only.text  # the missing permission is logged only
    assert "Staff role lacks permission manage_faqs" in caplog.text
    audit = production.client.get("/api/v1/admin/audit", headers=bearer(ALEX)).json()
    denied = [e for e in audit["events"] if e["action"] == "auth.access_denied"]
    assert len(denied) == 3
    assert error["retryable"] is False
    assert "y" * 43 not in caplog.text and MARIA not in caplog.text


# --- 9. FAQ index-refresh failure ------------------------------------------------------


def test_a_failed_index_refresh_keeps_the_edit_and_is_reported_not_hidden(
    production: Production, caplog: pytest.LogCaptureFixture
) -> None:
    semantic: SemanticSearchService = production.app.state.semantic_search_service
    semantic.build_index()
    assert production.client.get(READY).json()["checks"]["semantic_search"] == "ready"
    failures = counter(production, "faq_index_refreshes", outcome="failed")
    production.encoder.broken = True

    created = production.client.post(ADMIN_FAQS, json=NEW_FAQ, headers=bearer(ALEX))

    assert created.status_code == 201, created.text  # committed regardless
    refresh = [r for r in caplog.records if "semantic_index_refresh_failed" in r.getMessage()]
    assert refresh and refresh[0].levelno == logging.ERROR
    # Readiness no longer claims the stale index is ready; chat stays available.
    ready = production.client.get(READY).json()
    assert ready["status"] == "ready" and ready["checks"]["semantic_search"] == "unavailable"
    assert counter(production, "faq_index_refreshes", outcome="failed") == failures + 1
    chat = production.chat()
    assert chat.status_code == 200  # keyword-only meanwhile, never the stale index
    assert_nothing_internal(chat)

    production.encoder.broken = False
    semantic.get_index()  # the next search rebuilds it
    assert production.client.get(READY).json()["checks"]["semantic_search"] == "ready"


# --- 10. Support ticket creation failure -----------------------------------------------


def test_a_failed_ticket_creation_is_a_retryable_503_and_a_retry_creates_one_ticket(
    production: Production, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    sid, headers = production.conversation()
    body = ticket_body(sid, uuid.uuid4())
    failures = counter(production, "ticket_submissions", outcome="failed")
    with monkeypatch.context() as broken:
        broken.setattr(
            production.app.state.ticket_repository, "create_ticket", raising(DISK, "create")
        )
        failed = production.client.post(TICKETS, json=body, headers=headers)

    error = safe_error(failed, 503, "SERVICE_UNAVAILABLE")
    assert error["retryable"] is True and "Retry-After" not in failed.headers
    assert "will not create a duplicate" in error["message"]
    assert "customer@example.com" not in caplog.text
    assert "Support storage unavailable on POST /api/v1/support/tickets: TicketStoreError" \
        in caplog.text  # fmt: skip

    first = production.client.post(TICKETS, json=body, headers=headers)
    again = production.client.post(TICKETS, json=body, headers=headers)
    assert first.status_code == 201 and again.status_code in (200, 201)
    assert again.json()["ticket_id"] == first.json()["ticket_id"]
    assert counter(production, "ticket_submissions", outcome="failed") == failures + 1


# --- Readiness and public status -------------------------------------------------------


def test_not_ready_is_retryable_and_the_public_status_names_nothing(
    production: Production, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.database.connection import DatabaseError

    def down() -> None:
        raise DatabaseError("cannot open C:/srv/data/conversations.db")

    monkeypatch.setattr(production.app.state.database, "check", down)

    for url in (READY, "/api/v1/status"):
        response = production.client.get(url)
        assert response.status_code == 503
        error = response.json()["error"]
        assert error["retryable"] is True
        assert error["request_id"] == response.headers["X-Request-ID"]
        assert "conversations.db" not in response.text
    assert "conversation_storage" not in production.client.get("/api/v1/status").text
    assert json.loads(production.client.get(READY).text)["checks"]["conversation_storage"] == (
        "unavailable"
    )
