"""OpenAPI schema and interactive documentation endpoints."""

from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import __version__
from app.core.config import Settings
from app.main import create_app

DOC_PATHS = ("/docs", "/redoc", "/openapi.json")


def test_openapi_schema_is_accessible(client: TestClient) -> None:
    response = client.get("/openapi.json")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.json()["openapi"].startswith("3.")


def test_openapi_schema_describes_api(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()

    assert schema["info"]["title"] == "AI Customer Support Chatbot API"
    assert schema["info"]["version"] == __version__
    assert {"/", "/api/v1/health"} <= schema["paths"].keys()
    content = schema["paths"]["/api/v1/health"]["get"]["responses"]["200"]["content"]
    assert content["application/json"]["schema"]["$ref"].endswith("/HealthResponse")


@pytest.mark.parametrize("path", ["/docs", "/redoc"])
def test_interactive_docs_are_served(client: TestClient, path: str) -> None:
    response = client.get(path)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")


@pytest.mark.parametrize("path", DOC_PATHS)
def test_docs_are_disabled_in_production(
    make_settings: Callable[..., Settings], path: str, tmp_path: Path
) -> None:
    # Production refuses an in-memory database, so this app gets a temporary file.
    database_url = f"sqlite:///{(tmp_path / 'docs.db').as_posix()}"
    client = TestClient(create_app(make_settings(app_env="production", database_url=database_url)))

    assert client.get(path).status_code == 404
