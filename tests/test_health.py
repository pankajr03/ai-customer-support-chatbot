from fastapi.testclient import TestClient


def test_root_returns_app_info(client: TestClient) -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert response.json() == {"message": "AI Customer Support Chatbot API", "version": "0.1.0"}


def test_health_returns_healthy(client: TestClient) -> None:
    response = client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.json() == {"status": "healthy", "service": "ai-customer-support-chatbot"}


def test_health_is_versioned(client: TestClient) -> None:
    assert client.get("/health").status_code == 404


def test_unknown_route_returns_404(client: TestClient) -> None:
    assert client.get("/api/v1/does-not-exist").status_code == 404
