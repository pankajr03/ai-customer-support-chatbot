"""GET /api/v1/health reports service liveness."""

from fastapi.testclient import TestClient

HEALTH_URL = "/api/v1/health"


def test_health_returns_200(client: TestClient) -> None:
    response = client.get(HEALTH_URL)

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"


def test_health_response_structure(client: TestClient) -> None:
    body = client.get(HEALTH_URL).json()

    assert set(body) == {"status", "service"}
    assert isinstance(body["status"], str)
    assert isinstance(body["service"], str)


def test_health_response_values(client: TestClient) -> None:
    body = client.get(HEALTH_URL).json()

    assert body == {"status": "healthy", "service": "ai-customer-support-chatbot"}


def test_health_is_only_served_under_versioned_prefix(client: TestClient) -> None:
    assert client.get("/health").status_code == 404


def test_unknown_route_returns_404(client: TestClient) -> None:
    assert client.get("/api/v1/does-not-exist").status_code == 404
