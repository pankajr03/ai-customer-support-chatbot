"""Every API operation is either deliberately public or refuses requests without valid
credentials (Stage 13, authentication review).

Enumerated from the OpenAPI schema (plus the two hidden legacy admin routes), so an
operation added later is covered automatically: it must be added to PUBLIC on purpose,
or it must refuse. The detailed behaviour of each refusal (wrong customer, wrong role,
expired or deleted sessions) is tested in tests/test_security_audit.py.
"""

import re

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.staff_auth import generate_staff_token, hash_staff_token
from app.main import create_app
from app.models.support_api import SupportTicketCreateRequest

# Deliberately reachable without credentials (method, path).
PUBLIC = {
    ("GET", "/"),
    ("GET", "/api/v1/health"),
    ("GET", "/api/v1/ready"),
    ("GET", "/api/v1/faqs"),
    ("GET", "/api/v1/faqs/categories"),
    ("GET", "/api/v1/faqs/search"),
    ("GET", "/api/v1/faqs/{faq_id}"),
    ("POST", "/api/v1/semantic-search"),
    ("POST", "/api/v1/hybrid-search"),
    ("POST", "/api/v1/answer"),
    ("POST", "/api/v1/conversations"),  # issues the conversation's token
}
HIDDEN_ROUTES = [("GET", "/api/v1/admin/overview"), ("GET", "/api/v1/admin/analytics")]
SAMPLE_ID = "3f2b8c1e-7a4d-4e9b-9c61-2d5f0a8e4b17"
# Valid bodies, so a refusal cannot come from body validation instead of authentication.
BODIES = {
    "/api/v1/conversations/{session_id}/chat": {"message": "Where is my order?"},
    # The request model's own documented example.
    "/api/v1/support/tickets": SupportTicketCreateRequest.model_config["json_schema_extra"][
        "examples"
    ][0],
}


@pytest.fixture(scope="module")
def client() -> TestClient:
    token = generate_staff_token()
    settings = Settings(
        _env_file=None,
        database_url="sqlite://",
        staff_token_hashes=[f"alice:admin:{hash_staff_token(token)}"],
    )
    return TestClient(create_app(settings))


def operations(client: TestClient) -> list[tuple[str, str]]:
    paths = client.get("/openapi.json").json()["paths"]
    found = [(method.upper(), path) for path, ops in paths.items() for method in ops]
    return found + HIDDEN_ROUTES


def call(client: TestClient, method: str, path: str, headers: dict[str, str]) -> object:
    url = re.sub(r"\{[^}]+\}", SAMPLE_ID, path)
    body = BODIES.get(path, {}) if method in ("POST", "PUT", "PATCH") else None
    return client.request(method, url, json=body, headers=headers)


def test_every_operation_is_public_on_purpose_or_requires_credentials(
    client: TestClient,
) -> None:
    unprotected = set()
    for method, path in operations(client):
        response = call(client, method, path, {})
        if (method, path) in PUBLIC:
            assert response.status_code != 401, (method, path)
            continue
        if response.status_code != 401:
            unprotected.add((method, path, response.status_code))
            continue
        code = response.json()["error"]["code"]
        assert code in ("UNAUTHORIZED_SESSION", "STAFF_AUTH_REQUIRED"), (method, path, code)
        assert response.headers.get("WWW-Authenticate", "").startswith("Bearer")

    assert unprotected == set()
    assert PUBLIC <= set(operations(client))  # the public list names real operations


@pytest.mark.parametrize(
    "authorization",
    ["Bearer garbage", f"Bearer {generate_staff_token()}", "Basic c3RhZmY6cGFzc3dvcmQ="],
    ids=["garbage", "unknown-staff-token", "basic-auth"],
)
def test_no_protected_operation_accepts_invalid_credentials(
    client: TestClient, authorization: str
) -> None:
    for method, path in operations(client):
        if (method, path) in PUBLIC:
            continue
        response = call(client, method, path, {"Authorization": authorization})
        assert response.status_code in (401, 403, 404), (method, path, response.status_code)
        assert "customer@example.com" not in response.text
