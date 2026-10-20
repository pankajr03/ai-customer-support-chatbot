"""Concurrency and reliability: one message at a time, idempotent retries, failures.

"Two processes" are simulated with two independent ``Database`` objects (separate
engines and connection pools) on the same SQLite file: they share nothing in memory, so
anything that holds across them is enforced by the database itself.
"""

import sqlite3
import threading
import uuid
from collections.abc import Callable, Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import openai
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import text

from app.core.config import Settings
from app.database.connection import Database
from app.database.faq_repository import FAQFileNotFoundError
from app.main import create_app
from app.models.answer import GenerationMethod
from app.models.conversation import MessageRole
from app.repositories.conversation_repository import ConversationRepository, ConversationStoreError
from app.services.answer_decision_service import AnswerDecisionService, Decision
from app.services.context_builder import ContextBuilder
from app.services.conversation_service import (
    BUSY_RETRY_AFTER_SECONDS,
    ConversationBusyError,
    ConversationService,
)
from app.services.customer_chat_service import (
    IdempotencyKeyMismatchError,
    KnowledgeBaseUnavailableError,
)
from app.services.embedding_service import EmbeddingModelLoadError, EmbeddingService
from app.services.faq_service import FAQService
from app.services.grounded_answer_service import GroundedAnswerService
from app.services.llm_service import LLMService
from app.services.semantic_search_service import SemanticSearchService
from tests.fakes import FAKE_MODEL_NAME, FAQS, CountingLoader, FakeHybrid, FakeKeyword
from tests.test_customer_chat_service import (
    KEY,
    NOW,
    TTL,
    FakeClient,
    RoutingHybrid,
    Setup,
    ask,
    build,
)

LEASE = timedelta(seconds=120)
RETURN_POLICY = "What is your return policy?"


def file_url(tmp_path: Path) -> str:
    return f"sqlite:///{(tmp_path / 'conversations.db').as_posix()}"


@pytest.fixture
def database() -> Iterator[Database]:
    db = Database("sqlite://")
    db.init_schema()
    yield db
    db.dispose()


class GatedGrounded:
    """Wraps a grounded-answer service; blocks inside the 'LLM call' until released."""

    def __init__(self, inner: GroundedAnswerService) -> None:
        self.inner = inner
        self.entered = threading.Event()
        self.release = threading.Event()
        self.during: Callable[[], None] | None = None

    def answer(self, *args: Any, **kwargs: Any) -> Any:
        self.entered.set()
        if self.during is not None:
            self.during()
        assert self.release.wait(5), "test did not release the gate"
        return self.inner.answer(*args, **kwargs)


def gated(database: Database) -> tuple[Setup, GatedGrounded]:
    plain = build(database)
    gate = GatedGrounded(plain.chat._grounded)
    plain.chat._grounded = gate  # type: ignore[assignment]
    return plain, gate


def send_in_thread(s: Setup, message: str, **kwargs: Any) -> tuple[threading.Thread, list]:
    outcome: list = []

    def run() -> None:
        try:
            outcome.append(s.send(message, **kwargs))
        except Exception as exc:  # noqa: BLE001 - inspected by the test
            outcome.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    return thread, outcome


# --- Processing lease -----------------------------------------------------------------


def test_lease_allows_one_turn_at_a_time(database: Database) -> None:
    s = build(database)
    sid, token = s.created.session.session_id, s.created.access_token
    service = s.conversations

    service.begin_turn(sid, token, "first", now=NOW)
    with pytest.raises(ConversationBusyError) as other:
        service.begin_turn(sid, token, "second", now=NOW)
    with pytest.raises(ConversationBusyError) as same:
        service.begin_turn(sid, token, "first", now=NOW)

    assert not other.value.duplicate and same.value.duplicate
    # Check back soon, not after the whole lease (a crash-safety bound, not the wait).
    assert other.value.retry_after_seconds == BUSY_RETRY_AFTER_SECONDS < LEASE.total_seconds()
    assert not s.repo.release_processing(sid, "second")  # only the holder can release
    service.end_turn(sid, "first")
    service.begin_turn(sid, token, "second", now=NOW)  # free again


def test_expired_lease_can_be_taken_over(database: Database) -> None:
    s = build(database)
    sid, token = s.created.session.session_id, s.created.access_token

    s.conversations.begin_turn(sid, token, "crashed", now=NOW)  # never released
    with pytest.raises(ConversationBusyError):
        s.conversations.begin_turn(sid, token, "next", now=NOW + LEASE - timedelta(seconds=1))
    s.conversations.begin_turn(sid, token, "next", now=NOW + LEASE)

    assert not s.repo.release_processing(sid, "crashed")  # the old holder lost it


def test_storing_the_exchange_releases_the_lease(database: Database) -> None:
    s = build(database)

    s.send(RETURN_POLICY)
    s.send(RETURN_POLICY)  # would be 409 if the first turn had kept the lease

    assert [m.sequence for m in s.stored()] == [1, 2, 3, 4]


def test_failed_turn_releases_the_lease(database: Database) -> None:
    s = build(database)
    real = s.chat._grounded

    class Broken:
        def answer(self, *_: Any, **__: Any) -> Any:
            raise FAQFileNotFoundError("missing", path=Path("faqs.json"))

    s.chat._grounded = Broken()  # type: ignore[assignment]
    with pytest.raises(KnowledgeBaseUnavailableError):
        s.send(RETURN_POLICY)
    s.chat._grounded = real

    assert s.send(RETURN_POLICY).response.assistant_message.decision is Decision.ANSWERED


def test_lease_release_failure_is_not_fatal(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = build(database)

    def broken(*_: Any) -> bool:
        raise ConversationStoreError("down")

    monkeypatch.setattr(s.repo, "release_processing", broken)

    s.conversations.end_turn(s.created.session.session_id, "key")  # logs, never raises


# --- Simultaneous messages ------------------------------------------------------------


def test_second_simultaneous_message_is_refused_not_interleaved(database: Database) -> None:
    s, gate = gated(database)

    first, outcome = send_in_thread(s, RETURN_POLICY)
    assert gate.entered.wait(5)
    with pytest.raises(ConversationBusyError):
        s.send("How do I reset my password?")
    gate.release.set()
    first.join(5)

    assert outcome[0].response.assistant_message.source_faq_ids == ["faq_106"]
    s.send("How do I reset my password?")  # the retry after the first finished
    stored = s.stored()
    assert [(m.sequence, m.role) for m in stored] == [
        (1, MessageRole.USER),
        (2, MessageRole.ASSISTANT),
        (3, MessageRole.USER),
        (4, MessageRole.ASSISTANT),
    ]
    assert stored[1].source_faq_ids == ("faq_106",) and stored[3].source_faq_ids == ("faq_107",)


def test_lease_holds_across_independent_database_engines(tmp_path: Path) -> None:
    """Like two worker processes: no shared memory, one SQLite file."""
    first_db, second_db = Database(file_url(tmp_path)), Database(file_url(tmp_path))
    first_db.init_schema()
    first = ConversationService(ConversationRepository(first_db), TTL)
    second = ConversationService(ConversationRepository(second_db), TTL)
    created = first.create_conversation(now=NOW)
    sid, token = created.session.session_id, created.access_token

    first.begin_turn(sid, token, "worker-1", now=NOW)
    with pytest.raises(ConversationBusyError):
        second.begin_turn(sid, token, "worker-2", now=NOW)
    first.end_turn(sid, "worker-1")
    second.begin_turn(sid, token, "worker-2", now=NOW)

    first_db.dispose()
    second_db.dispose()


def test_no_transaction_is_open_while_the_answer_is_prepared(tmp_path: Path) -> None:
    database = Database(file_url(tmp_path))
    database.init_schema()
    s, gate = gated(database)
    # Another "process" with a short busy timeout: it fails fast if the lock were held.
    other_db = Database(file_url(tmp_path), busy_timeout_seconds=0.2)
    other = ConversationService(ConversationRepository(other_db), TTL)
    writes: list[object] = []
    gate.during = lambda: writes.append(other.create_conversation())

    thread, _ = send_in_thread(s, RETURN_POLICY)
    assert gate.entered.wait(5)
    gate.release.set()
    thread.join(5)

    assert len(writes) == 1  # a write succeeded while the first request was mid-answer
    other_db.dispose()
    database.dispose()


# --- Idempotency ----------------------------------------------------------------------


def test_repeated_idempotency_key_replays_the_stored_exchange(database: Database) -> None:
    s = build(database)

    first = s.send(RETURN_POLICY, idempotency_key="req-1")
    again = s.send(f"  {RETURN_POLICY} ", idempotency_key="req-1")

    assert again.replayed and not first.replayed
    assert again.response.user_message.message_id == first.response.user_message.message_id
    assert again.response.assistant_message == first.response.assistant_message
    assert again.response.generation_method is first.response.generation_method
    assert again.response.usage is None and again.decision is None
    assert len(s.stored()) == 2  # stored once
    assert len(s.hybrid.calls) == 1  # answered once


def test_idempotency_key_reused_for_another_message_is_refused(database: Database) -> None:
    s = build(database)
    s.send(RETURN_POLICY, idempotency_key="req-1")

    with pytest.raises(IdempotencyKeyMismatchError):
        s.send("How do I reset my password?", idempotency_key="req-1")
    assert len(s.stored()) == 2


def test_idempotency_keys_are_per_conversation(database: Database) -> None:
    s = build(database)
    other = s.conversations.create_conversation(now=NOW)

    s.send(RETURN_POLICY, idempotency_key="same")
    s.chat.chat(
        other.session.session_id,
        other.access_token,
        ask("How do I reset my password?"),
        idempotency_key="same",
        now=NOW,
    )

    assert len(s.repo.list_messages(other.session.session_id)) == 2


def test_duplicate_submitted_while_first_is_in_progress(database: Database) -> None:
    s, gate = gated(database)

    first, outcome = send_in_thread(s, RETURN_POLICY, idempotency_key="req-1")
    assert gate.entered.wait(5)
    with pytest.raises(ConversationBusyError) as busy:
        s.send(RETURN_POLICY, idempotency_key="req-1")
    gate.release.set()
    first.join(5)

    assert busy.value.duplicate
    replay = s.send(RETURN_POLICY, idempotency_key="req-1")
    assert replay.replayed
    assert replay.response.assistant_message == outcome[0].response.assistant_message
    assert len(s.stored()) == 2


def test_failed_request_can_be_retried_with_the_same_key(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = build(database)
    original = s.repo.add_exchange
    calls = {"n": 0}

    def fail_once(*args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConversationStoreError("disk full")
        return original(*args, **kwargs)

    monkeypatch.setattr(s.repo, "add_exchange", fail_once)

    with pytest.raises(ConversationStoreError):
        s.send(RETURN_POLICY, idempotency_key="req-1")
    assert s.stored() == []

    retried = s.send(RETURN_POLICY, idempotency_key="req-1")
    assert not retried.replayed and len(s.stored()) == 2


def test_key_stored_by_a_racing_request_is_replayed_not_duplicated(database: Database) -> None:
    """If the replay check misses a just-stored key (only possible after a lease expired),
    storing fails on the key and the stored result is returned instead."""
    s = build(database)
    s.send(RETURN_POLICY, idempotency_key="req-1")
    real_find = s.conversations.find_exchange
    seen = {"n": 0}

    def miss_first_time(*args: Any) -> Any:
        seen["n"] += 1
        return None if seen["n"] == 1 else real_find(*args)

    s.conversations.find_exchange = miss_first_time  # type: ignore[method-assign]

    turn = s.send(RETURN_POLICY, idempotency_key="req-1")

    assert turn.replayed and len(s.stored()) == 2


# --- Failures during a turn -----------------------------------------------------------


def test_llm_timeout_falls_back_to_the_approved_answer(database: Database) -> None:
    timeout = openai.APITimeoutError(
        request=httpx.Request("POST", "https://api.openai.com/v1/responses")
    )
    client = FakeClient(timeout)
    llm = LLMService(KEY, model="gpt-5.4-mini", max_retries=0, client_factory=lambda key: client)  # type: ignore[arg-type,return-value]
    grounded = GroundedAnswerService(
        AnswerDecisionService(RoutingHybrid(), FakeKeyword()),  # type: ignore[arg-type]
        ContextBuilder(),
        llm,
    )
    s = build(database, grounded=grounded)

    turn = s.send(RETURN_POLICY)

    assert len(client.calls) == 1
    assert turn.response.generation_method is GenerationMethod.APPROVED_FAQ
    assert s.stored()[1].content == FAQS["faq_106"].answer
    assert s.stored()[1].generation_method is GenerationMethod.APPROVED_FAQ


def test_retrieval_failure_gives_and_stores_the_safe_message(database: Database) -> None:
    hybrid = FakeHybrid([], EmbeddingModelLoadError("model missing"))
    grounded = GroundedAnswerService(
        AnswerDecisionService(hybrid, FakeKeyword()),  # type: ignore[arg-type]
        ContextBuilder(),
        LLMService(KEY, model="m", enabled=False),
    )
    s = build(database, grounded=grounded)

    turn = s.send(RETURN_POLICY)

    assert turn.response.assistant_message.decision is Decision.RETRIEVAL_UNAVAILABLE
    assert turn.response.generation_method is GenerationMethod.FALLBACK
    assert s.stored()[1].decision is Decision.RETRIEVAL_UNAVAILABLE
    s.send(RETURN_POLICY)  # the lease was released: the customer can try again


def test_missing_history_is_not_an_error(database: Database) -> None:
    s = build(database)
    s.send(RETURN_POLICY)
    with database.write() as db:  # e.g. messages removed by hand
        db.execute(text("DELETE FROM conversation_messages"))

    turn = s.send("What about after that?")

    assert turn.rewrite is not None and not turn.rewrite.rewritten
    assert [m.sequence for m in s.stored()] == [1, 2]


# --- Restart --------------------------------------------------------------------------


def test_history_and_order_survive_a_restart(tmp_path: Path) -> None:
    before = Database(file_url(tmp_path))
    before.init_schema()
    s = build(before)
    s.send(RETURN_POLICY)
    before.dispose()  # the app stops

    after = Database(file_url(tmp_path))
    after.init_schema()
    restarted = build(after)
    restarted.created = s.created  # the client kept its session ID and token
    turn = restarted.send("What about after that?")

    assert turn.rewrite is not None and turn.rewrite.rewritten  # it saw the earlier turn
    assert [m.sequence for m in restarted.stored()] == [1, 2, 3, 4]
    after.dispose()


def test_lease_of_a_crashed_process_expires_after_restart(tmp_path: Path) -> None:
    crashed = Database(file_url(tmp_path))
    crashed.init_schema()
    s = build(crashed)
    sid, token = s.created.session.session_id, s.created.access_token
    s.conversations.begin_turn(sid, token, "lost", now=NOW)  # dies before storing
    crashed.dispose()

    after = Database(file_url(tmp_path))
    restarted = build(after)
    restarted.created = s.created
    with pytest.raises(ConversationBusyError):
        restarted.send(RETURN_POLICY, now=NOW + timedelta(seconds=5))
    turn = restarted.send(RETURN_POLICY, now=NOW + LEASE)

    assert turn.response.assistant_message.decision is Decision.ANSWERED
    after.dispose()


# --- HTTP -----------------------------------------------------------------------------


def http_app(
    make_settings: Callable[..., Settings],
    faq_service: FAQService,
    database: Database | None = None,
) -> TestClient:
    semantic = SemanticSearchService(
        faq_service.list_active_faqs, EmbeddingService(FAKE_MODEL_NAME, loader=CountingLoader())
    )
    return TestClient(
        create_app(
            make_settings(),
            faq_service=faq_service,
            semantic_search_service=semantic,
            database=database,
        )
    )


def start(api: TestClient) -> tuple[str, dict[str, str]]:
    body = api.post("/api/v1/conversations").json()
    return body["session_id"], {"Authorization": f"Bearer {body['access_token']}"}


def test_http_idempotent_replay(
    make_settings: Callable[..., Settings], faq_service: FAQService
) -> None:
    question = FAQS["faq_107"].question
    with http_app(make_settings, faq_service) as api:
        sid, auth = start(api)
        url = f"/api/v1/conversations/{sid}/chat"
        headers = {**auth, "Idempotency-Key": "4d6f6c1e-retry"}

        first = api.post(url, json={"message": question}, headers=headers)
        again = api.post(url, json={"message": question}, headers=headers)
        mismatch = api.post(url, json={"message": "Something else?"}, headers=headers)
        bad_key = api.post(
            url, json={"message": question}, headers={**auth, "Idempotency-Key": "bad key!"}
        )
        total = api.get(f"/api/v1/conversations/{sid}/messages", headers=auth).json()

    assert first.status_code == again.status_code == 200
    assert "Idempotent-Replayed" not in first.headers
    assert again.headers["Idempotent-Replayed"] == "true"
    assert again.json()["assistant_message"] == first.json()["assistant_message"]
    assert mismatch.status_code == 422
    assert mismatch.json()["error"]["code"] == "DUPLICATE_MESSAGE_CONFLICT"
    assert bad_key.status_code == 422
    assert total["total_messages"] == 2


def test_http_busy_conversation_is_409_with_retry_after(
    make_settings: Callable[..., Settings], faq_service: FAQService
) -> None:
    with http_app(make_settings, faq_service) as api:
        sid, auth = start(api)
        service = api.app.state.conversation_service  # type: ignore[attr-defined]
        token = auth["Authorization"].removeprefix("Bearer ")
        service.begin_turn(sid, token, "in-flight")  # another request is mid-answer

        busy = api.post(f"/api/v1/conversations/{sid}/chat", json={"message": "Hi"}, headers=auth)
        duplicate = api.post(
            f"/api/v1/conversations/{sid}/chat",
            json={"message": "Hi"},
            headers={**auth, "Idempotency-Key": "in-flight"},
        )

    assert busy.status_code == 409
    assert busy.headers["Retry-After"] == duplicate.headers["Retry-After"] == "2"
    assert busy.json()["error"]["code"] == "REQUEST_IN_PROGRESS"
    assert "Another message" in busy.json()["error"]["message"]
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "REQUEST_IN_PROGRESS"
    assert "This message is still being answered" in duplicate.json()["error"]["message"]


def test_the_customer_page_waits_out_a_busy_conversation_by_itself(
    make_settings: Callable[..., Settings], faq_service: FAQService
) -> None:
    """Stage 13 regression: a busy conversation used to answer Retry-After 180 (the
    whole lease), longer than the page's 5-second automatic wait, so the customer saw
    an error even though the other message was answered within a second."""
    from frontend.api_client import ChatApiClient, Credentials

    with http_app(make_settings, faq_service) as api:
        sid, auth = start(api)
        token = auth["Authorization"].removeprefix("Bearer ")
        service = api.app.state.conversation_service  # type: ignore[attr-defined]
        service.begin_turn(sid, token, "another-message")  # still being answered
        waits: list[float] = []

        def sleep(seconds: float) -> None:  # the other message finishes meanwhile
            waits.append(seconds)
            service.end_turn(uuid.UUID(sid), "another-message")

        reply = ChatApiClient(http_client=api, sleep=sleep).send_message(
            Credentials(session_id=sid, access_token=token),
            FAQS["faq_107"].question,
            uuid.uuid4(),
        )

    assert waits == [BUSY_RETRY_AFTER_SECONDS]  # retried on its own after 2 s
    assert reply.answer


def test_http_locked_database_is_503_with_retry_after(
    make_settings: Callable[..., Settings], faq_service: FAQService, tmp_path: Path
) -> None:
    database = Database(file_url(tmp_path), busy_timeout_seconds=0.1)
    with http_app(make_settings, faq_service, database) as api:
        blocker = sqlite3.connect(tmp_path / "conversations.db", isolation_level=None)
        blocker.execute("BEGIN IMMEDIATE")  # another process holds the write lock
        try:
            response = api.post("/api/v1/conversations")
        finally:
            blocker.execute("ROLLBACK")
            blocker.close()
        after = api.post("/api/v1/conversations")

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "1"
    assert response.json()["error"]["code"] == "SERVICE_UNAVAILABLE"
    assert after.status_code == 201  # recovers once the lock is gone


# --- Settings -------------------------------------------------------------------------


def test_lease_must_outlast_the_slowest_answer() -> None:
    key = "sk-test-0123456789abcdefghij"
    with pytest.raises(ValidationError, match="CONVERSATION_PROCESSING_LEASE_SECONDS"):
        Settings(
            _env_file=None,
            llm_enabled=True,
            openai_api_key=key,
            openai_timeout_seconds=60,
            conversation_processing_lease_seconds=200,
        )
    ok = Settings(
        _env_file=None,
        llm_enabled=True,
        openai_api_key=key,
        openai_timeout_seconds=60,
        query_rewrite_use_llm=False,
        conversation_processing_lease_seconds=200,
    )
    assert ok.conversation_processing_lease_seconds == 200
    assert Settings(_env_file=None).conversation_processing_lease_seconds == 180


def test_unknown_session_lease_release_is_harmless(database: Database) -> None:
    assert not ConversationRepository(database).release_processing(uuid.uuid4(), "k")


# --- Edge paths -----------------------------------------------------------------------


def test_assistant_messages_cannot_carry_an_idempotency_key() -> None:
    from app.models.conversation import ConversationMessage

    with pytest.raises(ValidationError, match="idempotency_key"):
        ConversationMessage(
            message_id=uuid.uuid4(),
            session_id=uuid.uuid4(),
            sequence=2,
            role=MessageRole.ASSISTANT,
            content="Hello",
            created_at=NOW,
            decision=Decision.ANSWERED,
            idempotency_key="k",
        )


@pytest.mark.parametrize("change", ["closed", "expired"])
def test_claim_racing_a_close_or_expiry_is_refused(database: Database, change: str) -> None:
    from app.models.conversation import SessionStatus
    from app.services.conversation_service import (
        ConversationExpiredError,
        ConversationNotActiveError,
    )

    s = build(database)
    sid, token = s.created.session.session_id, s.created.access_token
    original = s.repo.claim_processing

    def changed_meanwhile(*args: Any, **kwargs: Any) -> None:
        if change == "closed":
            s.repo.set_status(sid, SessionStatus.CLOSED, now=NOW)
        else:
            kwargs["now"] = NOW + TTL  # the conversation expired just before the claim
        original(*args, **kwargs)

    s.repo.claim_processing = changed_meanwhile  # type: ignore[method-assign]

    expected = ConversationNotActiveError if change == "closed" else ConversationExpiredError
    with pytest.raises(expected):
        s.conversations.begin_turn(sid, token, "k", now=NOW)


def test_incomplete_stored_exchange_is_reported(database: Database) -> None:
    s = build(database)
    s.send(RETURN_POLICY, idempotency_key="req-1")
    with database.write() as db:
        db.execute(text("DELETE FROM conversation_messages WHERE role = 'assistant'"))

    with pytest.raises(ConversationStoreError, match="incomplete"):
        s.send(RETURN_POLICY, idempotency_key="req-1")


@pytest.mark.parametrize(
    ("decision", "sources", "expected"),
    [
        ("answered", '["faq_106"]', GenerationMethod.APPROVED_FAQ),
        ("ambiguous", '["faq_106", "faq_107"]', GenerationMethod.CLARIFICATION),
        ("no_match", "[]", GenerationMethod.FALLBACK),
        ("retrieval_unavailable", "[]", GenerationMethod.FALLBACK),
    ],
)
def test_replay_of_rows_without_a_stored_method(
    database: Database, decision: str, sources: str, expected: GenerationMethod
) -> None:
    s = build(database)
    s.send(RETURN_POLICY, idempotency_key="req-1")
    with database.write() as db:  # as stored before generation_method existed
        db.execute(
            text(
                "UPDATE conversation_messages SET generation_method = NULL, "
                "decision = :decision, source_faq_ids = :sources WHERE role = 'assistant'"
            ),
            {"decision": decision, "sources": sources},
        )

    response = s.send(RETURN_POLICY, idempotency_key="req-1").response

    assert response.generation_method is expected  # never claims an LLM was used
    assert response.decision == decision


def test_invalid_processing_lease(database: Database) -> None:
    with pytest.raises(ValueError, match="processing_lease"):
        ConversationService(ConversationRepository(database), TTL, processing_lease=timedelta(0))


def test_cleanup_survives_a_failed_checkpoint(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = build(database)
    s.conversations.create_conversation(now=NOW - 2 * TTL)

    def broken() -> None:
        raise ConversationStoreError("busy")

    monkeypatch.setattr(s.repo, "checkpoint", broken)

    assert s.conversations.cleanup_expired(now=NOW) == 1
