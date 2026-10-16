"""GET /api/v1/ready (Stage 9): can the service answer chat requests right now?

Readiness depends on local dependencies only (FAQ knowledge base, conversation storage).
The embedding model and the LLM are reported but never loaded, downloaded or called.
"""

from collections.abc import Callable
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core.config import Settings, describe_settings_error, get_settings
from app.database.connection import Database, DatabaseError
from app.main import create_app
from app.services.embedding_service import EmbeddingError, EmbeddingService
from app.services.faq_service import FAQService
from app.services.llm_service import CircuitBreaker, ErrorCategory
from app.services.semantic_search_service import SemanticSearchService
from tests.fakes import FAKE_MODEL_NAME, CountingLoader

READY_URL = "/api/v1/ready"
FAKE_KEY = "sk-test-" + "k" * 40


def _refuse_to_load(name: str) -> Any:
    raise AssertionError("readiness must never load the embedding model")


def _broken_loader(name: str) -> Any:
    raise OSError("no network")


@pytest.fixture
def make_app(
    make_settings: Callable[..., Settings], faq_service: FAQService
) -> Callable[..., FastAPI]:
    def _make(loader: Callable[[str], Any] = _refuse_to_load, **settings: Any) -> FastAPI:
        semantic = SemanticSearchService(
            faq_service.list_active_faqs,
            EmbeddingService(FAKE_MODEL_NAME, loader=loader, load_retry_seconds=30),
        )
        return create_app(
            make_settings(**settings), faq_service=faq_service, semantic_search_service=semantic
        )

    return _make


def test_a_fresh_backend_is_ready_without_loading_anything(
    make_app: Callable[..., FastAPI],
) -> None:
    app = make_app()
    with TestClient(app) as client:
        response = client.get(READY_URL)

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "status": "ready",
        "service": "ai-customer-support-chatbot",
        "checks": {
            "knowledge_base": "ok",
            "conversation_storage": "ok",
            "semantic_search": "not_loaded",  # loads on the first chat message
            "llm": "disabled",
        },
    }
    assert not app.state.semantic_search_service.is_ready


def test_semantic_search_is_ready_once_the_index_is_built(
    make_app: Callable[..., FastAPI],
) -> None:
    app = make_app(loader=CountingLoader())
    with TestClient(app) as client:
        app.state.semantic_search_service.build_index()
        checks = client.get(READY_URL).json()["checks"]
    assert checks["semantic_search"] == "ready"


def test_a_failed_model_load_is_reported_but_chat_stays_ready(
    make_app: Callable[..., FastAPI],
) -> None:
    app = make_app(loader=_broken_loader)
    with TestClient(app) as client:
        with pytest.raises(EmbeddingError):
            app.state.semantic_search_service.build_index()
        response = client.get(READY_URL)

    # Chat still answers (keyword-only, or retrieval_unavailable): ready, but limited.
    assert response.status_code == 200
    assert response.json()["checks"]["semantic_search"] == "unavailable"


def test_unusable_storage_makes_the_service_not_ready(make_app: Callable[..., FastAPI]) -> None:
    app = make_app()
    with TestClient(app) as client:
        with app.state.database.engine.begin() as connection:
            connection.exec_driver_sql("DROP TABLE conversation_messages")
        response = client.get(READY_URL)

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert body["checks"]["conversation_storage"] == "unavailable"
    # The standard error object too, so generic error handling works.
    assert body["error"]["code"] == "SERVICE_UNAVAILABLE"
    assert body["error"]["request_id"] == response.headers["x-request-id"]
    assert "conversation_messages" not in response.text  # no internals


def test_no_active_faqs_makes_the_service_not_ready(
    make_app: Callable[..., FastAPI], monkeypatch: pytest.MonkeyPatch
) -> None:
    app = make_app()
    monkeypatch.setattr(app.state.faq_service, "list_active_faqs", lambda: [])
    with TestClient(app) as client:
        response = client.get(READY_URL)
    assert response.status_code == 503
    assert response.json()["checks"]["knowledge_base"] == "unavailable"


def test_the_llm_is_reported_without_calling_it(make_app: Callable[..., FastAPI]) -> None:
    app = make_app(llm_enabled=True, openai_api_key=FAKE_KEY)
    llm = app.state.llm_service
    with TestClient(app) as client:
        assert client.get(READY_URL).json()["checks"]["llm"] == "enabled"
        llm.circuit_breaker.record_failure(ErrorCategory.AUTHENTICATION)
        paused = client.get(READY_URL)

    assert paused.status_code == 200  # approved FAQ answers are still given
    assert paused.json()["checks"]["llm"] == "paused"
    assert not llm.client_created  # no OpenAI client, so no paid request
    assert FAKE_KEY not in paused.text


def test_readiness_is_not_rate_limited(make_app: Callable[..., FastAPI]) -> None:
    with TestClient(make_app(rate_limit_requests_per_minute=1)) as client:
        assert all(client.get(READY_URL).status_code == 200 for _ in range(5))


# --- The status helpers it relies on ---------------------------------------------------


def test_database_check_reads_nothing_and_reports_missing_tables() -> None:
    database = Database("sqlite://")
    with pytest.raises(DatabaseError):
        database.check()  # no tables yet
    database.init_schema()
    database.init_schema()  # repeatable
    database.check()


def test_a_breaker_status_check_does_not_half_open_it() -> None:
    now = [0.0]
    breaker = CircuitBreaker(cooldown_seconds=10, clock=lambda: now[0])
    breaker.record_failure(ErrorCategory.AUTHENTICATION)
    assert breaker.is_open
    now[0] = 11
    assert not breaker.is_open
    assert breaker._failures == 1  # unchanged: only remaining_seconds() half-opens


def test_a_failed_model_load_is_remembered_for_the_retry_period() -> None:
    now = [0.0]
    service = EmbeddingService(
        FAKE_MODEL_NAME, loader=_broken_loader, load_retry_seconds=30, clock=lambda: now[0]
    )
    assert not service.load_failed_recently
    with pytest.raises(EmbeddingError):
        service.load()
    assert service.load_failed_recently
    now[0] = 31
    assert not service.load_failed_recently  # the next use tries again


# --- Startup errors ---------------------------------------------------------------------


def test_invalid_settings_stop_startup_with_names_but_no_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.main

    monkeypatch.setenv("RATE_LIMIT_REQUESTS_PER_MINUTE", "0")
    monkeypatch.setenv("OPENAI_API_KEY", "short-secret")
    monkeypatch.setenv("LLM_ENABLED", "true")
    get_settings.cache_clear()
    try:
        with pytest.raises(SystemExit) as stopped:
            app.main._create_default_app()
    finally:
        get_settings.cache_clear()

    message = str(stopped.value)
    assert message.startswith("Cannot start:")
    assert "RATE_LIMIT_REQUESTS_PER_MINUTE" in message
    assert "short-secret" not in message


def test_settings_errors_name_model_level_problems_without_values() -> None:
    with pytest.raises(ValidationError) as caught:
        Settings(_env_file=None, llm_enabled=True, openai_api_key="tiny-secret-key")
    message = describe_settings_error(caught.value)
    assert "OPENAI_API_KEY looks incomplete" in message
    assert "tiny-secret-key" not in message


def test_a_missing_faq_file_stops_startup_with_a_clear_message(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    import app.main

    monkeypatch.setenv("FAQ_DATA_PATH", str(tmp_path / "missing.json"))
    get_settings.cache_clear()
    try:
        with pytest.raises(SystemExit) as stopped:
            app.main._create_default_app()
    finally:
        get_settings.cache_clear()
    assert "FAQ knowledge base failed to load" in str(stopped.value)
    assert "FAQ_DATA_PATH" in str(stopped.value)
