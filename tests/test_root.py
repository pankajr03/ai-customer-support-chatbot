"""GET / returns basic application information."""

from fastapi.testclient import TestClient

from app import __version__


def test_root_returns_200(client: TestClient) -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"


def test_root_returns_app_name_and_version(client: TestClient) -> None:
    response = client.get("/")

    assert response.json() == {
        "message": "AI Customer Support Chatbot API",
        "version": __version__,
    }
