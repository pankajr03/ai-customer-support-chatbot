"""Conversation REST API: authorization, status codes, pagination and the full pipeline."""

import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.api.dependencies import get_conversation_service, get_customer_chat_service
from app.api.errors import SESSION_NOT_FOUND_MESSAGE
from app.core.config import Settings
from app.database.connection import Database
from app.main import create_app
from app.models.answer import GenerationMethod
from app.models.chat import CustomerChatResponse
from app.models.conversation import (
    ConversationMessage,
    ConversationSession,
)
from app.services.answer_decision_service import Decision
from app.services.embedding_service import EmbeddingService
from app.services.faq_service import FAQService
from app.services.semantic_search_service import SemanticSearchService
from tests.fakes import FAKE_MODEL_NAME, FAQS, CountingLoader

BASE = "/api/v1/conversations"
QUESTION = FAQS["faq_107"].question  # an exact FAQ question: answered by Stage 4


def build_app(
    make_settings: Callable[..., Settings],
    faq_service: FAQService,
    database: Database | None = None,
    **settings: Any,
) -> FastAPI:
    semantic = SemanticSearchService(
        faq_service.list_active_faqs, EmbeddingService(FAKE_MODEL_NAME, loader=CountingLoader())
    )
    return create_app(
        make_settings(**settings),
        faq_service=faq_service,
        semantic_search_service=semantic,
        database=database,
    )


@pytest.fixture
def api(make_settings: Callable[..., Settings], faq_service: FAQService) -> Iterator[TestClient]:
    with TestClient(build_app(make_settings, faq_service)) as client:
        yield client


def create(api: TestClient) -> tuple[str, dict[str, str]]:
    body = api.post(BASE).json()
    return body["session_id"], {"Authorization": f"Bearer {body['access_token']}"}


def other_token(api: TestClient) -> dict[str, str]:
    return create(api)[1]


# --- Create ---------------------------------------------------------------------------


def test_create_returns_id_and_secret_token_once(api: TestClient) -> None:
    response = api.post(BASE)

    assert response.status_code == 201
    body = response.json()
    assert set(body) == {
        "session_id",
        "access_token",
        "token_type",
        "status",
        "created_at",
        "expires_at",
    }
    assert body["access_token"].startswith("cst_")
    assert body["token_type"] == "Bearer"  # noqa: S105 - the scheme name, not a secret
    assert body["status"] == "active"
    assert uuid.UUID(body["session_id"]).version == 4
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Location"].endswith(f"{BASE}/{body['session_id']}")

    # Never returned again.
    details = api.get(f"{BASE}/{body['session_id']}", headers=create_headers(body))
    assert "access_token" not in details.json()


def create_headers(body: dict[str, Any]) -> dict[str, str]:
    return {"Authorization": f"Bearer {body['access_token']}"}


def test_create_ignores_a_body(api: TestClient) -> None:
    assert api.post(BASE, json={"session_id": "mine", "access_token": "x"}).status_code == 201


# --- Authorization --------------------------------------------------------------------


AUTHORIZED_CALLS = [
    ("get", "{base}/{sid}", None),
    ("get", "{base}/{sid}/messages", None),
    ("post", "{base}/{sid}/chat", {"message": QUESTION}),
    ("post", "{base}/{sid}/close", None),
    ("delete", "{base}/{sid}", None),
]


@pytest.mark.parametrize(("method", "path", "body"), AUTHORIZED_CALLS)
def test_missing_token_is_401(api: TestClient, method: str, path: str, body: Any) -> None:
    sid, _ = create(api)

    response = api.request(method, path.format(base=BASE, sid=sid), json=body)

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


@pytest.mark.parametrize(("method", "path", "body"), AUTHORIZED_CALLS)
def test_wrong_token_and_unknown_session_are_identical_404s(
    api: TestClient, method: str, path: str, body: Any
) -> None:
    sid, headers = create(api)
    attempts = [
        (sid, other_token(api)),  # exists, another conversation's token
        (sid, {"Authorization": "Bearer cst_" + "a" * 43}),  # exists, made-up token
        (str(uuid.uuid4()), headers),  # does not exist
        ("not-a-session-id", headers),  # malformed
    ]

    bodies = []
    for session_id, auth in attempts:
        response = api.request(
            method, path.format(base=BASE, sid=session_id), json=body, headers=auth
        )
        assert response.status_code == 404
        error = response.json()["error"]
        bodies.append({k: v for k, v in error.items() if k != "request_id"})

    assert (
        bodies
        == [{"code": "SESSION_NOT_FOUND", "message": SESSION_NOT_FOUND_MESSAGE, "retryable": False}]
        * 4
    )
    assert api.get(f"{BASE}/{sid}", headers=headers).status_code == 200  # untouched


@pytest.mark.parametrize("header", ["Basic dXNlcjpwYXNz", "Bearer", "cst_token-without-scheme"])
def test_non_bearer_authorization_is_401(api: TestClient, header: str) -> None:
    sid, _ = create(api)

    assert api.get(f"{BASE}/{sid}", headers={"Authorization": header}).status_code == 401


@pytest.mark.parametrize("param", ["access_token", "token"])
def test_token_in_the_url_is_refused(api: TestClient, param: str) -> None:
    sid, headers = create(api)
    token = headers["Authorization"].removeprefix("Bearer ")

    response = api.get(f"{BASE}/{sid}?{param}={token}")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert "Authorization header" in response.json()["error"]["message"]


def test_overlong_session_id_is_rejected(api: TestClient) -> None:
    _, headers = create(api)

    assert api.get(f"{BASE}/{'a' * 65}", headers=headers).status_code == 422


# --- Read -----------------------------------------------------------------------------


def test_get_conversation_metadata(api: TestClient) -> None:
    sid, headers = create(api)

    response = api.get(f"{BASE}/{sid}", headers=headers)

    assert response.status_code == 200
    assert set(response.json()) == {
        "session_id",
        "status",
        "created_at",
        "updated_at",
        "expires_at",
    }
    assert response.headers["Cache-Control"] == "no-store"


def test_messages_are_paginated_oldest_first(api: TestClient) -> None:
    sid, headers = create(api)
    for _ in range(3):
        assert (
            api.post(f"{BASE}/{sid}/chat", json={"message": QUESTION}, headers=headers).status_code
            == 200
        )

    first = api.get(f"{BASE}/{sid}/messages?limit=4", headers=headers).json()
    rest = api.get(f"{BASE}/{sid}/messages?offset=4&limit=4", headers=headers).json()

    assert [m["role"] for m in first["messages"]] == ["user", "assistant"] * 2
    assert (first["total_messages"], first["offset"], first["limit"], first["has_more"]) == (
        6,
        0,
        4,
        True,
    )
    assert len(rest["messages"]) == 2 and not rest["has_more"]
    ids = [m["message_id"] for m in first["messages"] + rest["messages"]]
    assert len(set(ids)) == 6


@pytest.mark.parametrize("query", ["limit=0", "limit=101", "offset=-1", "limit=abc"])
def test_invalid_pagination_is_422(api: TestClient, query: str) -> None:
    sid, headers = create(api)

    assert api.get(f"{BASE}/{sid}/messages?{query}", headers=headers).status_code == 422


# --- Chat -----------------------------------------------------------------------------


def test_chat_answers_from_approved_faqs_and_stores_the_exchange(api: TestClient) -> None:
    sid, headers = create(api)

    response = api.post(f"{BASE}/{sid}/chat", json={"message": f"  {QUESTION} "}, headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert body["session_id"] == sid
    assert body["user_message"]["content"] == QUESTION
    assert body["assistant_message"]["content"] == FAQS["faq_107"].answer
    assert body["assistant_message"]["decision"] == "answered"
    assert body["assistant_message"]["source_faq_ids"] == ["faq_107"]
    assert body["generation_method"] == "approved_faq"  # LLM disabled in tests
    assert body["usage"] is None
    assert response.headers["Cache-Control"] == "no-store"

    history = api.get(f"{BASE}/{sid}/messages", headers=headers).json()
    assert [m["message_id"] for m in history["messages"]] == [
        body["user_message"]["message_id"],
        body["assistant_message"]["message_id"],
    ]


def test_chat_with_no_match_returns_the_fallback(api: TestClient) -> None:
    sid, headers = create(api)

    body = api.post(
        f"{BASE}/{sid}/chat", json={"message": "Do you sell submarines?"}, headers=headers
    ).json()

    assert body["assistant_message"]["decision"] in {"no_match", "ambiguous"}
    assert body["generation_method"] in {"fallback", "clarification"}


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"message": ""},
        {"message": "   "},
        {"message": "x" * 4001},  # MAX_CUSTOMER_MESSAGE_CHARACTERS (default 4000)
        {"message": "hi", "role": "assistant"},
        {"message": "hi", "role": "system"},
        {"message": "hi", "history": []},
        {"message": "hi", "category": "not-a-category"},
        {"message": 42},
    ],
)
def test_invalid_chat_bodies_are_422_and_store_nothing(api: TestClient, payload: dict) -> None:
    sid, headers = create(api)

    assert api.post(f"{BASE}/{sid}/chat", json=payload, headers=headers).status_code == 422
    assert api.get(f"{BASE}/{sid}/messages", headers=headers).json()["total_messages"] == 0


def test_chat_category_filter(api: TestClient) -> None:
    sid, headers = create(api)

    response = api.post(
        f"{BASE}/{sid}/chat", json={"message": QUESTION, "category": " Account "}, headers=headers
    )

    assert response.status_code == 200


def test_chat_on_closed_conversation_is_409(api: TestClient) -> None:
    sid, headers = create(api)
    api.post(f"{BASE}/{sid}/close", headers=headers)

    response = api.post(f"{BASE}/{sid}/chat", json={"message": QUESTION}, headers=headers)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "SESSION_CLOSED"
    assert "start a new conversation" in response.json()["error"]["message"]


def test_expired_conversation_is_gone_except_for_delete(api: TestClient) -> None:
    sid, headers = create(api)
    database: Database = api.app.state.database  # type: ignore[attr-defined]
    with database.write() as db:
        db.execute(
            text(
                "UPDATE conversation_sessions SET created_at = '2020-01-01 00:00:00', "
                "updated_at = '2020-01-01 00:00:00', expires_at = '2020-01-01 01:00:00'"
            )
        )

    response = api.post(f"{BASE}/{sid}/chat", json={"message": QUESTION}, headers=headers)

    assert response.status_code == 410
    assert response.json()["error"]["code"] == "SESSION_EXPIRED"
    assert "start a new conversation" in response.json()["error"]["message"]
    for method, path in [
        ("get", f"{BASE}/{sid}"),
        ("get", f"{BASE}/{sid}/messages"),
        ("post", f"{BASE}/{sid}/close"),
    ]:
        gone = api.request(method, path, headers=headers)
        assert gone.status_code == 410 and gone.headers["Cache-Control"] == "no-store"
    assert api.delete(f"{BASE}/{sid}", headers=headers).status_code == 204
    assert api.get(f"{BASE}/{sid}", headers=headers).status_code == 404


def test_chat_sets_retry_after_when_search_is_degraded(api: TestClient) -> None:
    sid, headers = create(api)
    session = ConversationSession.new()
    user = ConversationMessage.user_message(session.session_id, 1, "Hi")
    reply = ConversationMessage.assistant_message(
        session.session_id, 2, "Please try again shortly.", Decision.RETRIEVAL_UNAVAILABLE
    )
    turn = SimpleNamespace(
        response=CustomerChatResponse.from_records(user, reply, GenerationMethod.FALLBACK),
        retry_after_seconds=2.2,
        replayed=False,
    )
    fake = SimpleNamespace(chat=lambda *args, **kwargs: turn)
    api.app.dependency_overrides[get_customer_chat_service] = lambda: fake  # type: ignore[attr-defined]

    response = api.post(f"{BASE}/{sid}/chat", json={"message": "Hi"}, headers=headers)

    assert response.status_code == 200 and response.headers["Retry-After"] == "3"


# --- Close and delete -----------------------------------------------------------------


def test_close_is_idempotent_and_keeps_history_readable(api: TestClient) -> None:
    sid, headers = create(api)
    api.post(f"{BASE}/{sid}/chat", json={"message": QUESTION}, headers=headers)

    first = api.post(f"{BASE}/{sid}/close", headers=headers)
    second = api.post(f"{BASE}/{sid}/close", headers=headers)

    assert first.status_code == second.status_code == 200
    assert first.json()["status"] == "closed" and first.json() == second.json()
    assert api.get(f"{BASE}/{sid}/messages", headers=headers).json()["total_messages"] == 2


def test_delete_removes_conversation_and_messages(api: TestClient) -> None:
    sid, headers = create(api)
    api.post(f"{BASE}/{sid}/chat", json={"message": QUESTION}, headers=headers)

    response = api.delete(f"{BASE}/{sid}", headers=headers)

    assert response.status_code == 204 and response.content == b""
    assert api.get(f"{BASE}/{sid}", headers=headers).status_code == 404
    assert api.delete(f"{BASE}/{sid}", headers=headers).status_code == 404
    database: Database = api.app.state.database  # type: ignore[attr-defined]
    with database.read() as db:
        assert db.execute(text("SELECT count(*) FROM conversation_messages")).scalar() == 0


# --- Failures -------------------------------------------------------------------------


@pytest.fixture
def logged_api(
    capsys: pytest.CaptureFixture[str],
    make_settings: Callable[..., Settings],
    faq_service: FAQService,
) -> Callable[[], TestClient]:
    """Build the app inside the test body, after capsys, so its log output is captured
    (create_app reconfigures logging and binds the handler to the current stdout)."""
    return lambda: TestClient(build_app(make_settings, faq_service))


def test_storage_failure_is_503_without_details(
    logged_api: Callable[[], TestClient], capsys: pytest.CaptureFixture[str]
) -> None:
    with logged_api() as api:
        sid, headers = create(api)
        database: Database = api.app.state.database  # type: ignore[attr-defined]
        with database.write() as db:
            db.execute(text("DROP TABLE conversation_messages"))

        response = api.post(f"{BASE}/{sid}/chat", json={"message": QUESTION}, headers=headers)

    assert response.status_code == 503
    body = response.json()
    assert body["error"]["code"] == "SERVICE_UNAVAILABLE"
    assert "table" not in response.text.lower() and "sql" not in response.text.lower()
    log = capsys.readouterr().out
    assert "Conversation storage unavailable on POST" in log
    assert QUESTION not in log


def test_database_that_cannot_start_leaves_other_endpoints_working(
    make_settings: Callable[..., Settings],
    faq_service: FAQService,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / "taken.db").mkdir()  # a folder where the database file should be
    broken = Database(f"sqlite:///{(tmp_path / 'taken.db').as_posix()}")

    with TestClient(build_app(make_settings, faq_service, database=broken)) as client:
        assert client.get("/api/v1/health").status_code == 200
        assert client.post(BASE).status_code == 503

    assert "Conversation storage unavailable; conversation endpoints" in capsys.readouterr().out


# --- Security -------------------------------------------------------------------------


def test_tokens_never_reach_the_logs(
    logged_api: Callable[[], TestClient], capsys: pytest.CaptureFixture[str]
) -> None:
    with logged_api() as api:
        sid, headers = create(api)
        token = headers["Authorization"].removeprefix("Bearer ")

        api.get(f"{BASE}/{sid}", headers=headers)
        api.post(f"{BASE}/{sid}/chat", json={"message": QUESTION}, headers=headers)
        api.get(f"{BASE}/{sid}", headers={"Authorization": "Bearer cst_" + "b" * 43})
        api.get(f"{BASE}/{sid}?access_token={token}")
        api.delete(f"{BASE}/{sid}", headers=headers)

    log = capsys.readouterr().out
    # Requests are logged by route template (the session ID is not in access lines) ...
    assert "DELETE /api/v1/conversations/{session_id} -> 204" in log
    assert "event=conversation_turn" in log
    assert token not in log  # ... but never with a token
    assert "cst_" not in log


def test_cors_allows_the_authorization_header_and_delete(
    make_settings: Callable[..., Settings], faq_service: FAQService
) -> None:
    origin = "http://localhost:8501"
    with TestClient(build_app(make_settings, faq_service, cors_origins=[origin])) as client:
        response = client.options(
            f"{BASE}/x",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "DELETE",
                "Access-Control-Request-Headers": "authorization",
            },
        )

    assert response.status_code == 200
    assert "DELETE" in response.headers["access-control-allow-methods"]
    assert "authorization" in response.headers["access-control-allow-headers"].lower()


# --- OpenAPI --------------------------------------------------------------------------


def test_openapi_documents_bearer_authorization(api: TestClient) -> None:
    spec = api.get("/openapi.json").json()

    scheme = spec["components"]["securitySchemes"]["ConversationToken"]
    assert scheme == {**scheme, "type": "http", "scheme": "bearer"}
    assert "Never send it in a URL" in scheme["description"]
    paths = spec["paths"]
    assert "security" not in paths[BASE]["post"]  # creating needs no token
    for path, method in [
        (f"{BASE}/{{session_id}}", "get"),
        (f"{BASE}/{{session_id}}/messages", "get"),
        (f"{BASE}/{{session_id}}/chat", "post"),
        (f"{BASE}/{{session_id}}/close", "post"),
        (f"{BASE}/{{session_id}}", "delete"),
    ]:
        operation = paths[path][method]
        assert operation["security"] == [{"ConversationToken": []}]
        assert {"401", "404", "503"} <= set(operation["responses"])
    chat = paths[f"{BASE}/{{session_id}}/chat"]["post"]
    assert {"409", "410", "422"} <= set(chat["responses"])
    header = next(p for p in chat["parameters"] if p["name"] == "Idempotency-Key")
    assert header["in"] == "header" and not header["required"]


def test_existing_endpoints_are_unchanged(api: TestClient) -> None:
    assert api.get("/api/v1/health").status_code == 200
    assert api.post("/api/v1/answer", json={"query": QUESTION}).json()["source_faq_ids"] == [
        "faq_107"
    ]


@pytest.mark.parametrize(
    ("getter", "name"),
    [
        (get_conversation_service, "ConversationService"),
        (get_customer_chat_service, "CustomerChatService"),
    ],
)
def test_missing_services_fail_loudly(getter: Callable[..., Any], name: str) -> None:
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))

    with pytest.raises(RuntimeError, match=f"{name} is not initialised"):
        getter(request)


def test_page_arguments_are_validated(api: TestClient) -> None:
    repo = api.app.state.conversation_service._repo  # type: ignore[attr-defined]

    for offset, limit in [(-1, 10), (0, 0)]:
        with pytest.raises(ValueError, match="offset"):
            repo.get_messages_page(uuid.uuid4(), offset=offset, limit=limit)
