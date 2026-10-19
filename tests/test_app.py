"""Cross-cutting behaviour: request IDs, error handling, CORS."""

import logging
from collections.abc import Callable

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app


def test_response_has_request_id_header(client: TestClient) -> None:
    first = client.get("/api/v1/health").headers["X-Request-ID"]
    second = client.get("/api/v1/health").headers["X-Request-ID"]

    assert len(first) == 32
    assert first != second


def test_requests_are_logged_with_request_id(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="app.request"):
        response = client.get("/api/v1/faqs/categories")

    record = next(r for r in caplog.records if r.name == "app.request")
    assert "GET /api/v1/faqs/categories -> 200" in record.getMessage()
    assert record.request_id == response.headers["X-Request-ID"]


def test_unhandled_error_returns_safe_500(app: FastAPI) -> None:
    @app.get("/boom")
    def boom() -> None:
        raise RuntimeError("secret internal detail")

    with TestClient(app) as client:
        response = client.get("/boom")

    assert response.status_code == 500
    body = response.json()
    assert body["error"]["code"] == "INTERNAL_ERROR"
    assert body["error"]["request_id"] == response.headers["X-Request-ID"]
    assert "secret" not in response.text


def test_cors_allows_only_configured_origins(make_settings: Callable[..., Settings]) -> None:
    settings = make_settings(cors_origins=["http://localhost:8501"])
    client = TestClient(create_app(settings))

    allowed = client.get("/api/v1/health", headers={"Origin": "http://localhost:8501"})
    blocked = client.get("/api/v1/health", headers={"Origin": "https://evil.example"})

    assert allowed.headers["access-control-allow-origin"] == "http://localhost:8501"
    assert "access-control-allow-origin" not in blocked.headers


def test_no_cors_headers_by_default(client: TestClient) -> None:
    response = client.get("/api/v1/health", headers={"Origin": "http://localhost:8501"})

    assert "access-control-allow-origin" not in response.headers
