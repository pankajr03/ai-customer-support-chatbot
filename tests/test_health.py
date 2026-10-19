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


def test_health_is_not_cached_and_answers_head(client: TestClient) -> None:
    assert client.get(HEALTH_URL).headers["cache-control"] == "no-store"
    head = client.head(HEALTH_URL)
    assert head.status_code == 200
    assert head.content == b""


def test_health_stays_healthy_when_dependencies_are_not(client: TestClient) -> None:
    """Liveness checks nothing but the process: a broken database is readiness's job."""
    with client.app.state.database.engine.begin() as connection:  # type: ignore[attr-defined]
        connection.exec_driver_sql("DROP TABLE conversation_messages")

    assert client.get(HEALTH_URL).status_code == 200
    assert client.get("/api/v1/ready").status_code == 503
