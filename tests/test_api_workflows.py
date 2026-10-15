"""End-to-end API workflows (Stage 7, Task 12).

Every test drives the real application over HTTP (FastAPI's TestClient) with a temporary
SQLite file. Only the AI dependencies are replaced, deterministically:

- the embedding model by ``ScriptedEncoder`` (each FAQ question its own direction; a
  message points at the FAQs whose keyword it contains, or at nothing), so a message is
  answered, ambiguous or unmatched exactly as each test intends;
- the OpenAI client by ``ScriptedLLM``, which rewrites nothing: it returns the primary
  FAQ's approved answer as a valid grounded reply, or raises a scripted error.

No paid API is ever called; the LLM service's retries are off so no test sleeps.
"""

import html
import json
import re
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import numpy as np
import openai
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.core.config import Settings
from app.database.connection import Database
from app.main import create_app
from app.models.chat import CustomerChatResponse
from app.models.errors import ErrorResponse
from app.services.embedding_service import EmbeddingService
from app.services.faq_service import FAQService
from app.services.llm_service import LLMService
from app.services.semantic_search_service import SemanticSearchService
from tests.fakes import FAKE_MODEL_NAME, FAQS

BASE = "/api/v1/conversations"
KEY = SecretStr("sk-test-0123456789abcdefghij")
RESET = "I forgot my password, what now?"  # -> faq_107: answered
FOLLOW_UP = "How long does it take?"  # depends on the previous question
SHIPPING = "Tell me about shipping"  # -> faq_103 and faq_104: ambiguous
UNKNOWN = "Do you sell submarines?"  # -> nothing: no_match
RETURNS = "Can I send something back? return"  # -> faq_106: answered


# --- Deterministic AI stand-ins ---------------------------------------------------------


class ScriptedEncoder:
    """Embeddings with exactly the similarities a test needs."""

    ROUTES = (("password", ("faq_107",)), ("shipping", ("faq_103", "faq_104")),
              ("return", ("faq_106",)), ("track", ("faq_101",)))  # fmt: skip

    def __init__(self) -> None:
        self.ids = sorted(FAQS)
        self.dimension = len(self.ids) + 1  # the last direction means "nothing"
        self.questions = {faq.question: faq.id for faq in FAQS.values()}

    def get_embedding_dimension(self) -> int:
        return self.dimension

    def _vector(self, sentence: str) -> np.ndarray:
        vector = np.zeros(self.dimension)
        targets: tuple[str, ...] = ()
        if sentence in self.questions:
            targets = (self.questions[sentence],)
        else:
            lowered = sentence.lower()
            targets = next((ids for word, ids in self.ROUTES if word in lowered), ())
        for faq_id in targets:
            vector[self.ids.index(faq_id)] = 1.0
        if not targets:
            vector[-1] = 1.0
        return vector / np.linalg.norm(vector)

    def encode(self, sentences: list[str], **kwargs: Any) -> np.ndarray:
        return np.array([self._vector(sentence) for sentence in sentences])


_PRIMARY = re.compile(
    r'<faq id="(faq_\d+)"[^>]*role="primary">\s*<question>.*?</question>\s*'
    r"<answer>(.*?)</answer>",
    re.S,
)


class ScriptedLLM:
    """An OpenAI client stand-in. Replies with the primary FAQ's approved answer (a valid
    grounded reply), or raises ``error``. Records every request."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[dict[str, Any]] = []
        self.responses = SimpleNamespace(create=self._create)

    def _create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        match = _PRIMARY.search(str(kwargs["input"]))
        assert match, "the request carries the approved FAQ evidence"
        faq_id, answer = match.group(1), html.unescape(match.group(2))
        reply = {"status": "answered", "answer": answer, "source_faq_ids": [faq_id], "reason": "r"}
        return SimpleNamespace(
            status="completed",
            output_text=json.dumps(reply),
            output=[],
            model="gpt-5.4-mini",
            id="resp_1",
            usage=SimpleNamespace(input_tokens=300, output_tokens=40),
            _request_id="req_1",
        )

    def close(self) -> None:
        pass


def failing_loader(model_name: str) -> Any:
    raise OSError("model files not found")


class App:
    """A running app, its HTTP client, and its parts (for setup and checks only)."""

    def __init__(self, client: TestClient, database: Database, llm: ScriptedLLM | None) -> None:
        self.client, self.database, self.llm = client, database, llm

    def start(self) -> tuple[str, dict[str, str]]:
        response = self.client.post(BASE)
        assert response.status_code == 201
        body = response.json()
        return body["session_id"], {"Authorization": f"Bearer {body['access_token']}"}

    def chat(self, sid: str, headers: dict[str, str] | None, message: str, **body: Any) -> Any:
        return self.client.post(
            f"{BASE}/{sid}/chat", json={"message": message, **body}, headers=headers or {}
        )

    def stored(self, sid: str) -> int:
        with self.database.read() as db:
            return int(
                db.execute(
                    text("SELECT count(*) FROM conversation_messages WHERE session_id = :sid"),
                    {"sid": uuid.UUID(sid).hex},
                ).scalar()
            )


@pytest.fixture
def make_app(
    make_settings: Callable[..., Settings], faq_service: FAQService, tmp_path: Path
) -> Iterator[Callable[..., App]]:
    clients: list[TestClient] = []

    def _make(
        *,
        llm: ScriptedLLM | None = None,
        loader: Callable[[str], Any] | None = None,
        **settings: Any,
    ) -> App:
        database = Database(f"sqlite:///{(tmp_path / f'chat{len(clients)}.db').as_posix()}")
        encoder = ScriptedEncoder()
        semantic = SemanticSearchService(
            faq_service.list_active_faqs,
            EmbeddingService(FAKE_MODEL_NAME, loader=loader or (lambda name: encoder)),
        )
        llm_service = (
            LLMService(KEY, model="gpt-5.4-mini", max_retries=0, client_factory=lambda key: llm)
            if llm is not None
            else None
        )
        app = create_app(
            make_settings(query_rewrite_use_llm=False, **settings),
            faq_service=faq_service,
            semantic_search_service=semantic,
            database=database,
            llm_service=llm_service,  # type: ignore[arg-type]
        )
        client = TestClient(app)
        client.__enter__()
        clients.append(client)
        return App(client, database, llm)

    yield _make
    for client in clients:
        client.__exit__(None, None, None)


@pytest.fixture
def chat_app(make_app: Callable[..., App]) -> App:
    return make_app()


def error_code(response: Any, status: int) -> str:
    assert response.status_code == status, response.text
    ErrorResponse.model_validate(response.json())
    return str(response.json()["error"]["code"])


def contract(response: Any) -> dict[str, Any]:
    assert response.status_code == 200, response.text
    CustomerChatResponse.model_validate(response.json())  # the stable contract holds
    return dict(response.json())


# --- Session lifecycle ------------------------------------------------------------------


def test_session_lifecycle(chat_app: App) -> None:
    sid, headers = chat_app.start()

    first = contract(chat_app.chat(sid, headers, RESET))
    history = chat_app.client.get(f"{BASE}/{sid}/messages", headers=headers).json()
    follow_up = contract(chat_app.chat(sid, headers, FOLLOW_UP))
    closed = chat_app.client.post(f"{BASE}/{sid}/close", headers=headers)
    refused = chat_app.chat(sid, headers, "One more thing?")
    final = chat_app.client.get(f"{BASE}/{sid}/messages", headers=headers).json()

    assert first["decision"] == "answered" and first["answer"] == FAQS["faq_107"].answer
    assert [m["content"] for m in history["messages"]] == [RESET, FAQS["faq_107"].answer]
    # The follow-up has no topic of its own: the conversation resolved it to faq_107.
    assert follow_up["decision"] == "answered"
    assert [s["faq_id"] for s in follow_up["sources"]] == ["faq_107"]
    assert closed.status_code == 200 and closed.json()["status"] == "closed"
    assert error_code(refused, 409) == "SESSION_CLOSED"
    assert final["total_messages"] == 4 and final["status"] == "closed"  # still readable


def test_the_follow_up_alone_would_not_match(chat_app: App) -> None:
    sid, headers = chat_app.start()

    assert contract(chat_app.chat(sid, headers, FOLLOW_UP))["decision"] == "no_match"


# --- Idempotency ------------------------------------------------------------------------


def test_repeated_identical_request_is_replayed(make_app: Callable[..., App]) -> None:
    llm = ScriptedLLM()
    app = make_app(llm=llm)
    sid, headers = app.start()
    key = str(uuid.uuid4())

    first = app.chat(sid, headers, RESET, client_message_id=key)
    again = app.chat(sid, headers, RESET, client_message_id=key)

    assert contract(again)["message_id"] == contract(first)["message_id"]
    assert again.headers["Idempotent-Replayed"] == "true"
    assert again.json()["usage"] is None and first.json()["usage"] is not None
    assert len(llm.calls) == 1  # no second generation
    assert app.stored(sid) == 2


def test_same_id_with_different_content_is_refused(chat_app: App) -> None:
    sid, headers = chat_app.start()
    key = str(uuid.uuid4())
    chat_app.chat(sid, headers, RESET, client_message_id=key)

    other_text = chat_app.chat(sid, headers, RETURNS, client_message_id=key)
    other_category = chat_app.chat(sid, headers, RESET, client_message_id=key, category="orders")

    assert error_code(other_text, 422) == "DUPLICATE_MESSAGE_CONFLICT"
    assert error_code(other_category, 422) == "DUPLICATE_MESSAGE_CONFLICT"
    assert chat_app.stored(sid) == 2


def test_duplicate_while_processing_then_retry_after_persistence(chat_app: App) -> None:
    sid, headers = chat_app.start()
    key = str(uuid.uuid4())
    service = chat_app.client.app.state.conversation_service  # type: ignore[attr-defined]
    token = headers["Authorization"].removeprefix("Bearer ")
    service.begin_turn(sid, token, key)  # the first submission is still being answered

    duplicate = chat_app.chat(sid, headers, RESET, client_message_id=key)
    assert error_code(duplicate, 409) == "REQUEST_IN_PROGRESS"
    assert "Retry-After" in duplicate.headers
    assert chat_app.stored(sid) == 0

    service.end_turn(uuid.UUID(sid), key)  # the first submission failed: nothing stored
    processed = contract(chat_app.chat(sid, headers, RESET, client_message_id=key))
    replayed = chat_app.chat(sid, headers, RESET, client_message_id=key)

    assert replayed.headers["Idempotent-Replayed"] == "true"
    assert contract(replayed)["message_id"] == processed["message_id"]
    assert chat_app.stored(sid) == 2


def test_database_enforces_unique_retry_keys(chat_app: App) -> None:
    sid, headers = chat_app.start()
    key = str(uuid.uuid4())
    chat_app.chat(sid, headers, RESET, client_message_id=key)

    with pytest.raises(IntegrityError), chat_app.database.write() as db:
        db.execute(
            text(
                "INSERT INTO conversation_messages (message_id, session_id, sequence, role, "
                "content, created_at, source_faq_ids, idempotency_key) VALUES (:id, :sid, 50, "
                "'user', 'x', '2026-10-15 09:30:00', '[]', :key)"
            ),
            {"id": uuid.uuid4().hex, "sid": uuid.UUID(sid).hex, "key": key},
        )


# --- Authorization ----------------------------------------------------------------------


def test_authorization(chat_app: App) -> None:
    sid, headers = chat_app.start()
    other_sid, other_headers = chat_app.start()
    bogus = {"Authorization": "Bearer cst_" + "a" * 43}

    assert contract(chat_app.chat(sid, headers, RESET))["decision"] == "answered"  # valid
    assert error_code(chat_app.chat(sid, None, RESET), 401) == "UNAUTHORIZED_SESSION"
    assert error_code(chat_app.chat(sid, bogus, RESET), 404) == "SESSION_NOT_FOUND"
    # Another conversation's valid token: exactly the same answer as an unknown session.
    cross = chat_app.chat(sid, other_headers, RESET)
    unknown = chat_app.chat(str(uuid.uuid4()), headers, RESET)
    assert error_code(cross, 404) == error_code(unknown, 404) == "SESSION_NOT_FOUND"
    assert cross.json()["error"]["message"] == unknown.json()["error"]["message"]
    reads = chat_app.client.get(f"{BASE}/{sid}/messages", headers=other_headers)
    assert error_code(reads, 404) == "SESSION_NOT_FOUND"
    assert chat_app.stored(sid) == 2 and chat_app.stored(other_sid) == 0


def test_expired_session(chat_app: App) -> None:
    sid, headers = chat_app.start()
    with chat_app.database.write() as db:
        db.execute(
            text(
                "UPDATE conversation_sessions SET created_at = '2020-01-01 00:00:00', "
                "updated_at = '2020-01-01 00:00:00', expires_at = '2020-01-01 01:00:00'"
            )
        )

    assert error_code(chat_app.chat(sid, headers, RESET), 410) == "SESSION_EXPIRED"
    gone = chat_app.client.get(f"{BASE}/{sid}/messages", headers=headers)
    assert error_code(gone, 410) == "SESSION_EXPIRED"
    assert chat_app.client.delete(f"{BASE}/{sid}", headers=headers).status_code == 204


def test_deleted_session(chat_app: App) -> None:
    sid, headers = chat_app.start()
    chat_app.chat(sid, headers, RESET)

    assert chat_app.client.delete(f"{BASE}/{sid}", headers=headers).status_code == 204
    assert error_code(chat_app.chat(sid, headers, RESET), 404) == "SESSION_NOT_FOUND"
    assert error_code(chat_app.client.get(f"{BASE}/{sid}", headers=headers), 404)
    assert chat_app.stored(sid) == 0


# --- Response contract ------------------------------------------------------------------


def test_answered_with_valid_sources(chat_app: App) -> None:
    sid, headers = chat_app.start()

    body = contract(chat_app.chat(sid, headers, RESET))

    assert body["decision"] == "answered" and body["generation_method"] == "approved_faq"
    assert body["sources"] == [{"faq_id": "faq_107", "question": FAQS["faq_107"].question}]
    for source in body["sources"]:  # every source is an approved, active FAQ
        assert FAQS[source["faq_id"]].is_active
        assert FAQS[source["faq_id"]].question == source["question"]


def test_no_match(chat_app: App) -> None:
    sid, headers = chat_app.start()

    body = contract(chat_app.chat(sid, headers, UNKNOWN))

    assert (body["decision"], body["generation_method"], body["sources"]) == (
        "no_match",
        "fallback",
        [],
    )
    assert "support team" in body["answer"]


def test_ambiguous(chat_app: App) -> None:
    sid, headers = chat_app.start()

    body = contract(chat_app.chat(sid, headers, SHIPPING))

    assert (body["decision"], body["generation_method"]) == ("ambiguous", "clarification")
    assert {s["faq_id"] for s in body["sources"]} == {"faq_103", "faq_104"}
    assert body["answer"].startswith("Could you clarify")


def test_retrieval_unavailable(make_app: Callable[..., App]) -> None:
    app = make_app(loader=failing_loader)
    sid, headers = app.start()

    response = app.chat(sid, headers, "Tell me something nice")
    body = contract(response)

    assert (body["decision"], body["generation_method"], body["sources"]) == (
        "retrieval_unavailable",
        "fallback",
        [],
    )
    assert "try again" in body["answer"]
    assert app.stored(sid) == 2  # a normal, stored reply


def test_llm_answer_has_usage_metadata(make_app: Callable[..., App]) -> None:
    llm = ScriptedLLM()
    app = make_app(llm=llm)
    sid, headers = app.start()

    body = contract(app.chat(sid, headers, RESET))

    assert body["generation_method"] == "llm_grounded"
    assert body["usage"]["input_tokens"] == 300 and body["usage"]["output_tokens"] == 40
    assert len(llm.calls) == 1 and llm.calls[0]["store"] is False


def test_llm_failure_falls_back_to_the_approved_faq(make_app: Callable[..., App]) -> None:
    timeout = openai.APITimeoutError(request=httpx.Request("POST", "https://api.openai.com"))
    app = make_app(llm=ScriptedLLM(error=timeout))
    sid, headers = app.start()

    body = contract(app.chat(sid, headers, RESET))

    assert body["decision"] == "answered" and body["generation_method"] == "approved_faq"
    assert body["answer"] == FAQS["faq_107"].answer
    assert body["usage"] is None  # no LLM response was received


def test_usage_is_absent_without_an_llm(chat_app: App) -> None:
    sid, headers = chat_app.start()

    assert contract(chat_app.chat(sid, headers, RESET))["usage"] is None


# --- Error handling ---------------------------------------------------------------------


def test_invalid_requests(chat_app: App) -> None:
    sid, headers = chat_app.start()
    url = f"{BASE}/{sid}/chat"
    json_headers = {**headers, "Content-Type": "application/json"}

    invalid_json = chat_app.client.post(url, content=b'{"message": ', headers=json_headers)
    empty = chat_app.chat(sid, headers, "   ")
    oversized = chat_app.chat(sid, headers, "x" * 4001)
    bad_uuid = chat_app.chat(sid, headers, RESET, client_message_id="1234")

    for response in (invalid_json, empty, oversized, bad_uuid):
        assert error_code(response, 422) == "VALIDATION_ERROR"
    assert oversized.json()["error"]["details"][0]["type"] == "string_too_long"
    assert bad_uuid.json()["error"]["details"][0]["loc"] == ["body", "client_message_id"]
    assert chat_app.stored(sid) == 0


def test_database_failure(chat_app: App) -> None:
    sid, headers = chat_app.start()
    with chat_app.database.write() as db:
        db.execute(text("DROP TABLE conversation_messages"))

    response = chat_app.chat(sid, headers, RESET)

    assert error_code(response, 503) == "SERVICE_UNAVAILABLE"
    assert "table" not in response.text.lower() and "sql" not in response.text.lower()


def test_concurrent_request_conflict(chat_app: App) -> None:
    sid, headers = chat_app.start()
    service = chat_app.client.app.state.conversation_service  # type: ignore[attr-defined]
    service.begin_turn(sid, headers["Authorization"].removeprefix("Bearer "), "other-request")

    response = chat_app.chat(sid, headers, RESET)

    assert error_code(response, 409) == "REQUEST_IN_PROGRESS"
    assert 1 <= int(response.headers["Retry-After"]) <= 180
    assert chat_app.stored(sid) == 0


# --- Regression: earlier stages ---------------------------------------------------------


def test_earlier_endpoints_still_work(make_app: Callable[..., App]) -> None:
    client = make_app().client

    assert client.get("/api/v1/health").json()["status"] == "healthy"
    faqs = client.get("/api/v1/faqs").json()
    assert faqs["total"] == len([f for f in FAQS.values() if f.is_active])
    assert client.get("/api/v1/faqs/faq_107").json()["question"] == FAQS["faq_107"].question
    assert client.get("/api/v1/faqs/search", params={"q": "password"}).status_code == 200
    assert client.get("/api/v1/faqs/categories").status_code == 200
    semantic = client.post("/api/v1/semantic-search", json={"query": RESET}).json()
    assert semantic["results"][0]["faq_id"] == "faq_107"
    hybrid = client.post("/api/v1/hybrid-search", json={"query": RESET}).json()
    assert hybrid["results"][0]["faq_id"] == "faq_107"
    answer = client.post("/api/v1/answer", json={"query": RESET}).json()
    assert (answer["decision"], answer["source_faq_ids"]) == ("answered", ["faq_107"])


def test_stage6_conversation_endpoints(chat_app: App) -> None:
    sid, headers = chat_app.start()
    chat_app.chat(sid, headers, RESET, client_message_id=str(uuid.uuid4()))

    details = chat_app.client.get(f"{BASE}/{sid}", headers=headers)
    page = chat_app.client.get(f"{BASE}/{sid}/messages", params={"limit": 1}, headers=headers)
    legacy = chat_app.client.post(
        f"{BASE}/{sid}/chat",
        json={"message": RETURNS},
        headers={**headers, "Idempotency-Key": "stage6-key"},
    )

    assert details.json()["status"] == "active"
    assert page.json()["has_more"] is True and len(page.json()["messages"]) == 1
    assert legacy.status_code == 200
    assert legacy.json()["assistant_message"]["source_faq_ids"] == ["faq_106"]  # deprecated
    assert legacy.json()["user_message"]["content"] == RETURNS


def test_openapi_documentation(chat_app: App) -> None:
    schema = chat_app.client.get("/openapi.json").json()

    paths = set(schema["paths"])
    assert {
        "/api/v1/health",
        "/api/v1/faqs",
        "/api/v1/semantic-search",
        "/api/v1/hybrid-search",
        "/api/v1/answer",
        BASE,
        f"{BASE}/{{session_id}}/chat",
        f"{BASE}/{{session_id}}/messages",
    } <= paths
    assert chat_app.client.get("/docs").status_code == 200
    assert "ConversationToken" in schema["components"]["securitySchemes"]
