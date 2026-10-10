import pytest
from fastapi.testclient import TestClient

from app.schemas.chat import MAX_MESSAGE_LENGTH
from app.services.chat_service import PLACEHOLDER_REPLY


def test_chat_returns_reply_and_new_session(client: TestClient) -> None:
    response = client.post("/api/v1/chat", json={"message": "Where is my order?"})

    assert response.status_code == 200
    body = response.json()
    assert body["reply"] == PLACEHOLDER_REPLY
    assert len(body["session_id"]) == 32
    assert "created_at" in body


def test_chat_keeps_provided_session_id(client: TestClient) -> None:
    response = client.post(
        "/api/v1/chat", json={"message": "Hello", "session_id": "customer-42_abc"}
    )

    assert response.status_code == 200
    assert response.json()["session_id"] == "customer-42_abc"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"message": ""},
        {"message": "   "},
        {"message": "x" * (MAX_MESSAGE_LENGTH + 1)},
        {"message": "Hi", "session_id": "bad id with spaces"},
        {"message": "Hi", "session_id": "a" * 65},
        {"message": 123},
    ],
    ids=[
        "missing-message",
        "empty",
        "whitespace-only",
        "too-long",
        "session-bad-chars",
        "session-too-long",
        "non-string",
    ],
)
def test_chat_rejects_invalid_input(client: TestClient, payload: dict) -> None:
    response = client.post("/api/v1/chat", json=payload)

    assert response.status_code == 422


def test_chat_accepts_max_length_message(client: TestClient) -> None:
    response = client.post("/api/v1/chat", json={"message": "x" * MAX_MESSAGE_LENGTH})

    assert response.status_code == 200
