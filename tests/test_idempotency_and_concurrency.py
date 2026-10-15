"""Idempotent message submission and concurrent chat requests (Stage 7, Tasks 6 and 7).

Retries: a ``client_message_id`` (or ``Idempotency-Key``) is unique per conversation in
the database; an identical retry returns the stored reply without a second LLM call; the
same ID for a different request (message or category) is refused.

Concurrency: one message per conversation at a time (a database lease, 409 otherwise),
and a turn that outlived its lease cannot store its reply after a newer message.
"""

import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from app.api.dependencies import get_customer_chat_service
from app.api.errors import CHANGED_MESSAGE
from app.core.config import Settings
from app.database.connection import Database
from app.models.chat import ChatDecision
from app.models.conversation import ConversationMessage
from app.repositories.conversation_repository import ConversationRepository
from app.services.answer_decision_service import Decision
from app.services.conversation_service import ConversationChangedError, ConversationService
from app.services.customer_chat_service import (
    IdempotencyKeyMismatchError,
    is_same_request,
    request_fingerprint,
)
from app.services.faq_service import FAQService
from tests.test_conversations_api import BASE, build_app, create
from tests.test_customer_chat_service import NOW, TTL, ask, build, reply

RETURN_POLICY = "What is your return policy?"
CLIENT_ID = "4c981eb1-7847-41c0-bf6b-0a0e1b9d6d91"


@pytest.fixture
def database() -> Iterator[Database]:
    db = Database("sqlite://")
    db.init_schema()
    yield db
    db.dispose()


# --- Request fingerprints ---------------------------------------------------------------


def test_fingerprints_cover_the_message_and_the_category() -> None:
    base = request_fingerprint("Where is my order?", None)

    assert len(base) == 64 and int(base, 16) >= 0
    assert request_fingerprint("  Where  is my\norder?", None) == base  # spacing ignored
    assert request_fingerprint("Where is my order", None) != base  # punctuation counts
    assert request_fingerprint("where is my order?", None) != base  # so does case
    assert request_fingerprint("Where is my order?", "shipping") != base
    assert request_fingerprint("Where is my order?", "orders") != request_fingerprint(
        "Where is my order?", "shipping"
    )


def test_rows_without_a_fingerprint_are_compared_by_text() -> None:
    legacy = ConversationMessage.user_message(uuid.uuid4(), 1, "Hi  there", now=NOW)

    assert is_same_request(legacy, request_fingerprint("anything", "orders"), "Hi there")
    assert not is_same_request(legacy, request_fingerprint("Hi there", None), "Bye")


# --- Task 6: idempotent submission ----------------------------------------------------


def test_an_identical_retry_returns_the_original_reply_without_a_second_llm_call(
    database: Database,
) -> None:
    llm = build(
        database,
        reply("You can return most unused items within 30 days of delivery.", "faq_106"),
        llm_enabled=True,
    )

    first = llm.send(RETURN_POLICY, idempotency_key=CLIENT_ID)
    again = llm.send(RETURN_POLICY, idempotency_key=CLIENT_ID)

    assert len(llm.client.calls) == 1  # the retry made no LLM call
    assert again.replayed and not first.replayed
    assert again.response.message_id == first.response.message_id
    assert again.response.answer == first.response.answer
    assert again.response.usage is None  # nothing was spent on the retry
    assert len(llm.stored()) == 2


def test_the_same_id_with_another_category_is_a_different_request(database: Database) -> None:
    s = build(database)
    s.send(RETURN_POLICY, idempotency_key=CLIENT_ID)

    with pytest.raises(IdempotencyKeyMismatchError):
        s.send(RETURN_POLICY, idempotency_key=CLIENT_ID, category="shipping")
    assert len(s.stored()) == 2
    assert s.hybrid.calls[-1]["category"] is None  # the refused request was never searched


def test_the_fingerprint_is_stored_with_the_customer_message_only(database: Database) -> None:
    s = build(database)
    s.send(RETURN_POLICY, idempotency_key=CLIENT_ID, category="returns")
    s.send("How do I reset my password?")  # no retry key: no fingerprint needed

    user, assistant, plain_user, _ = s.stored()
    assert user.request_fingerprint == request_fingerprint(RETURN_POLICY, "returns")
    assert assistant.request_fingerprint is None
    assert plain_user.request_fingerprint is None


def test_retry_keys_are_unique_per_conversation_in_the_database(database: Database) -> None:
    s = build(database)
    s.send(RETURN_POLICY, idempotency_key=CLIENT_ID)
    sid = s.created.session.session_id.hex

    with pytest.raises(IntegrityError), database.write() as db:  # bypassing the app
        db.execute(
            text(
                "INSERT INTO conversation_messages (message_id, session_id, sequence, role, "
                "content, created_at, source_faq_ids, idempotency_key) VALUES (:id, :sid, 99, "
                "'user', 'x', '2026-10-15 09:30:00', '[]', :key)"
            ),
            {"id": "f" * 32, "sid": sid, "key": CLIENT_ID},
        )


def test_the_same_id_in_another_conversation_is_independent(database: Database) -> None:
    s = build(database)
    other = s.conversations.create_conversation(now=NOW)
    s.send(RETURN_POLICY, idempotency_key=CLIENT_ID)

    turn = s.chat.chat(
        other.session.session_id,
        other.access_token,
        ask("How do I reset my password?"),
        idempotency_key=CLIENT_ID,
        now=NOW,
    )

    assert not turn.replayed
    assert turn.response.decision is ChatDecision.ANSWERED


# --- Schema migration (an existing Stage 6 database) ----------------------------------


def test_an_existing_database_gains_the_column_and_keeps_its_data(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'conversations.db').as_posix()}"
    old = Database(url)
    old.init_schema()
    s = build(old)
    s.send(RETURN_POLICY, idempotency_key="stage6-key")
    with old.write() as db:  # make it a Stage 6 file: no request_fingerprint column
        db.execute(text("ALTER TABLE conversation_messages DROP COLUMN request_fingerprint"))
    old.dispose()

    upgraded = Database(url)
    upgraded.init_schema()  # what the app runs at startup

    columns = {c["name"] for c in inspect(upgraded.engine).get_columns("conversation_messages")}
    assert "request_fingerprint" in columns
    repo = ConversationRepository(upgraded)
    stored = repo.list_messages(s.created.session.session_id)
    assert [m.content for m in stored][0] == RETURN_POLICY  # nothing was lost
    assert stored[0].request_fingerprint is None
    conversations = ConversationService(repo, TTL)
    found = conversations.find_exchange(
        s.created.session.session_id, s.created.access_token, "stage6-key"
    )
    assert found is not None and is_same_request(found[0], "0" * 64, RETURN_POLICY)
    upgraded.dispose()


# --- Task 7: a turn that outlived its lease --------------------------------------------


class TakeOver:
    """While the first turn is being answered, its lease expires and another message is
    answered and stored (as a slow LLM call past CONVERSATION_PROCESSING_LEASE_SECONDS
    would allow)."""

    def __init__(self, s: Any, *, other_message: str | None) -> None:
        self.s, self.real, self.other = s, s.chat._grounded, other_message
        self.done = False

    def answer(self, *args: Any, **kwargs: Any) -> Any:
        result = self.real.answer(*args, **kwargs)
        if not self.done:
            self.done = True
            with self.s.repo._db.write() as db:
                db.execute(
                    text("UPDATE conversation_sessions SET processing_until = :past"),
                    {"past": "2000-01-01 00:00:00"},
                )
            if self.other is not None:
                self.s.send(self.other)  # takes the expired lease, answers, stores
        return result


def test_a_reply_is_not_stored_after_a_newer_message(database: Database) -> None:
    s = build(database)
    s.chat._grounded = TakeOver(s, other_message="How do I reset my password?")

    with pytest.raises(ConversationChangedError):
        s.send(RETURN_POLICY, idempotency_key="slow")

    stored = s.stored()
    assert [m.content for m in stored][::2] == ["How do I reset my password?"]
    assert [m.sequence for m in stored] == [1, 2]  # nothing interleaved or out of order
    retry = s.send(RETURN_POLICY, idempotency_key="slow")  # nothing was stored: answer again
    assert not retry.replayed
    assert [m.sequence for m in s.stored()] == [1, 2, 3, 4]


def test_an_expired_lease_nobody_took_still_stores(database: Database) -> None:
    s = build(database)
    s.chat._grounded = TakeOver(s, other_message=None)

    turn = s.send(RETURN_POLICY)

    assert turn.response.decision is ChatDecision.ANSWERED
    assert len(s.stored()) == 2
    with s.repo._db.read() as db:  # and the lease is released
        assert db.execute(text("SELECT processing_key FROM conversation_sessions")).scalar() is None


def test_the_stored_order_is_the_order_replies_were_stored(database: Database) -> None:
    s = build(database)
    for message in (RETURN_POLICY, "How do I reset my password?", "Tell me about delivery"):
        s.send(message)

    stored = s.stored()
    assert [m.sequence for m in stored] == list(range(1, 7))
    assert [m.role.value for m in stored] == ["user", "assistant"] * 3
    assert stored[5].decision is Decision.AMBIGUOUS


# --- HTTP -----------------------------------------------------------------------------


@pytest.fixture
def api(make_settings: Callable[..., Settings], faq_service: FAQService) -> Iterator[TestClient]:
    with TestClient(build_app(make_settings, faq_service)) as client:
        yield client


def chat(api: TestClient, sid: str, headers: dict[str, str], **body: Any) -> Any:
    return api.post(f"{BASE}/{sid}/chat", json=body, headers=headers)


def test_http_retry_with_another_category_is_a_duplicate_message_conflict(
    api: TestClient,
) -> None:
    sid, headers = create(api)
    first = chat(api, sid, headers, message=RETURN_POLICY, client_message_id=CLIENT_ID)

    again = chat(api, sid, headers, message=RETURN_POLICY, client_message_id=CLIENT_ID)
    other = chat(
        api, sid, headers, message=RETURN_POLICY, client_message_id=CLIENT_ID, category="orders"
    )

    assert again.status_code == 200 and again.headers["Idempotent-Replayed"] == "true"
    assert again.json()["message_id"] == first.json()["message_id"]
    assert other.status_code == 422
    assert other.json()["error"]["code"] == "DUPLICATE_MESSAGE_CONFLICT"
    assert api.get(f"{BASE}/{sid}/messages", headers=headers).json()["total_messages"] == 2


def test_http_conversation_changed_is_409_request_in_progress(api: TestClient) -> None:
    sid, headers = create(api)

    def changed(*args: Any, **kwargs: Any) -> Any:
        raise ConversationChangedError()

    fake = SimpleNamespace(chat=changed)
    api.app.dependency_overrides[get_customer_chat_service] = lambda: fake  # type: ignore[attr-defined]

    response = chat(api, sid, headers, message=RETURN_POLICY)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "REQUEST_IN_PROGRESS"
    assert response.json()["error"]["message"] == CHANGED_MESSAGE
    assert response.headers["Cache-Control"] == "no-store"
