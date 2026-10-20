"""Shutdown and restart behaviour of the application (Stage 13, Task 21).

The container-level behaviour (signals, restart policy, health recovery, data surviving
a restart) is verified on Docker by ``scripts/evaluate_restarts.py``; results in
docs/shutdown-and-restart.md. These tests keep the application's part of it:

- shutdown releases the OpenAI client and the database connections;
- a processing lease left by a process that stopped mid-answer is released at the next
  start, so the customer's retry is answered at once instead of being refused for the
  rest of the lease (156-170 s observed on Docker before this fix);
- the stopped process's turn can never store its reply after that release, and the
  retry is answered exactly once.
"""

import logging
import uuid
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.core.config import Settings
from app.database.connection import Database
from app.main import create_app
from app.models.answer import GenerationMethod
from app.models.conversation import utc_now
from app.repositories.conversation_repository import (
    ConversationRepository,
    ProcessingLeaseLostError,
)
from app.services.answer_decision_service import Decision
from app.services.embedding_service import EmbeddingService
from app.services.faq_service import FAQService
from app.services.llm_service import LLMService
from app.services.semantic_search_service import SemanticSearchService
from tests.fakes import FAKE_MODEL_NAME, FAQS, CountingLoader

QUESTION = FAQS["faq_107"].question


def make_app(make_settings: Callable[..., Settings], faq_service: FAQService, path: Path,
             **kwargs: Any) -> Any:  # fmt: skip
    semantic = SemanticSearchService(
        faq_service.list_active_faqs, EmbeddingService(FAKE_MODEL_NAME, loader=CountingLoader())
    )
    database = Database(f"sqlite:///{path.as_posix()}")
    return create_app(make_settings(), faq_service=faq_service, semantic_search_service=semantic,
                      database=database, **kwargs)  # fmt: skip


def test_shutdown_closes_the_llm_client_and_the_database(
    make_settings: Callable[..., Settings], faq_service: FAQService, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    llm = LLMService(SecretStr("sk-test-" + "0" * 32), model="m", enabled=False)
    app = make_app(make_settings, faq_service, tmp_path / "db.sqlite", llm_service=llm)
    closed: list[str] = []
    monkeypatch.setattr(llm, "close", lambda: closed.append("llm"))
    dispose = app.state.database.dispose
    monkeypatch.setattr(app.state.database, "dispose", lambda: (closed.append("db"), dispose()))

    with TestClient(app) as client:
        assert client.get("/api/v1/health").status_code == 200
        assert closed == []

    assert closed == ["llm", "db"]


def start_turn(path: Path, key: str) -> tuple[uuid.UUID, str]:
    """A conversation whose message is being answered when the process stops."""
    database = Database(f"sqlite:///{path.as_posix()}")
    database.init_schema()
    repository = ConversationRepository(database)
    from app.services.conversation_service import ConversationService

    service = ConversationService(repository, ttl=timedelta(minutes=60))
    created = service.create_conversation()
    service.begin_turn(created.session.session_id, created.access_token, key)
    database.dispose()  # the process is gone; its lease stays in the file
    return created.session.session_id, created.access_token


def test_a_lease_left_by_a_stopped_process_is_released_at_the_next_start(
    make_settings: Callable[..., Settings], faq_service: FAQService, tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:  # fmt: skip
    path = tmp_path / "db.sqlite"
    key = str(uuid.uuid4())
    sid, token = start_turn(path, key)
    app = make_app(make_settings, faq_service, path)
    logging.getLogger().addHandler(caplog.handler)  # create_app reconfigured logging

    with caplog.at_level(logging.WARNING), TestClient(app) as client:
        retry = client.post(
            f"/api/v1/conversations/{sid}/chat",
            json={"message": QUESTION, "client_message_id": key},
            headers={"Authorization": f"Bearer {token}"},
        )
        again = client.post(
            f"/api/v1/conversations/{sid}/chat",
            json={"message": QUESTION, "client_message_id": key},
            headers={"Authorization": f"Bearer {token}"},
        )
        history = client.get(f"/api/v1/conversations/{sid}/messages",
                             headers={"Authorization": f"Bearer {token}"}).json()  # fmt: skip

    assert retry.status_code == 200  # was 409 REQUEST_IN_PROGRESS until the lease expired
    assert again.status_code == 200 and again.headers["Idempotent-Replayed"] == "true"
    assert again.json()["message_id"] == retry.json()["message_id"]
    assert history["total_messages"] == 2  # answered exactly once
    assert "Released 1 processing lease(s) left by a stopped process" in caplog.text


def test_the_stopped_turn_can_never_store_after_the_release(tmp_path: Path) -> None:
    path = tmp_path / "db.sqlite"
    key = str(uuid.uuid4())
    sid, _ = start_turn(path, key)
    database = Database(f"sqlite:///{path.as_posix()}")
    repository = ConversationRepository(database)

    assert repository.release_all_processing() == 1
    with pytest.raises(ProcessingLeaseLostError):  # the old turn, finishing late
        repository.add_exchange(
            sid, "Hi", "Reply", Decision.NO_MATCH, (), received_at=utc_now(),
            answered_at=utc_now(), processing_key=key,
            generation_method=GenerationMethod.FALLBACK,
        )  # fmt: skip
    assert repository.list_messages(sid) == []
    database.dispose()


def test_a_clean_start_releases_nothing(
    make_settings: Callable[..., Settings], faq_service: FAQService, tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:  # fmt: skip
    app = make_app(make_settings, faq_service, tmp_path / "db.sqlite")
    logging.getLogger().addHandler(caplog.handler)  # create_app reconfigured logging

    with caplog.at_level(logging.WARNING), TestClient(app):
        pass

    assert "processing lease" not in caplog.text
    assert app.state.conversation_service.release_abandoned_leases() == 0


def test_a_lease_taken_after_startup_is_respected(
    make_settings: Callable[..., Settings], faq_service: FAQService, tmp_path: Path,
) -> None:  # fmt: skip
    """Release happens once, at startup: a turn in progress later still holds its lease."""
    app = make_app(make_settings, faq_service, tmp_path / "db.sqlite")
    with TestClient(app) as client:
        created = client.post("/api/v1/conversations").json()
        sid, token = created["session_id"], created["access_token"]
        app.state.conversation_service.begin_turn(sid, token, "in-flight")
        busy = client.post(f"/api/v1/conversations/{sid}/chat", json={"message": QUESTION},
                           headers={"Authorization": f"Bearer {token}"})  # fmt: skip

    assert busy.status_code == 409 and busy.headers["Retry-After"] == "2"
