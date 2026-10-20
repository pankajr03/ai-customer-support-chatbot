"""Unauthorized admin access and safe error handling, on the running stack."""

import uuid

import httpx
import pytest

from app.models.errors import ErrorResponse
from tests.deployment.conftest import ADMIN_PORT, HTTPS_PORT, Stack

ADMIN_FAQS = "http://backend:8000/api/v1/admin/faqs"


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# --- Unauthorized admin access -----------------------------------------------------------


def test_the_dashboard_requires_its_password(stack: Stack) -> None:
    missing = stack.get(ADMIN_PORT, "/")
    wrong = stack.get(ADMIN_PORT, "/", auth=(stack.basic_auth[0], "not-the-password"))
    right = stack.get(ADMIN_PORT, "/", auth=stack.basic_auth)

    assert missing.status_code == 401 and wrong.status_code == 401
    assert right.status_code == 200
    assert stack.basic_auth[1] not in missing.text + wrong.text


def test_the_dashboard_listens_on_loopback_only(stack: Stack) -> None:
    published = stack.compose("port", "nginx", "9443").stdout.strip()

    assert published == f"127.0.0.1:{ADMIN_PORT}"


def test_the_staff_api_is_not_published(stack: Stack) -> None:
    public = stack.get(HTTPS_PORT, "/api/v1/admin/faqs")
    with_token = stack.get(HTTPS_PORT, "/api/v1/faqs", headers=bearer(stack.admin_token))
    dashboard = stack.get(ADMIN_PORT, "/api/v1/admin/faqs", auth=stack.basic_auth)

    assert public.status_code == 404 and public.json()["error"]["code"] == "NOT_FOUND"
    # A staff token is refused on the public site before any routing.
    assert with_token.status_code == 403
    assert with_token.json()["error"]["code"] == "STAFF_FORBIDDEN"
    assert dashboard.status_code == 404  # the dashboard calls the API server-side


def test_the_staff_api_refuses_missing_and_insufficient_tokens(stack: Stack) -> None:
    none, body_none, _ = stack.request_in("admin", "GET", ADMIN_FAQS)
    forged, _, _ = stack.request_in("admin", "GET", ADMIN_FAQS, headers=bearer("stf_" + "x" * 43))
    support, body_support, _ = stack.request_in(
        "admin", "GET", ADMIN_FAQS, headers=bearer(stack.support_token)
    )
    admin, body_admin, _ = stack.request_in(
        "admin", "GET", ADMIN_FAQS, headers=bearer(stack.admin_token)
    )

    assert none == 401 and body_none["error"]["code"] == "STAFF_AUTH_REQUIRED"
    assert forged == 401
    assert support == 403 and body_support["error"]["code"] == "STAFF_FORBIDDEN"
    assert admin == 200 and body_admin["total"] > 0


# --- Safe error handling -----------------------------------------------------------------


def assert_safe(response: httpx.Response) -> dict:
    body = response.json()
    ErrorResponse.model_validate(body)  # the API's error contract, retryable included
    for leak in ("Traceback", 'File "', ".py", "sqlite", "uvicorn", "OPENAI", "/app/"):
        assert leak not in response.text, leak
    return dict(body["error"])


def test_backend_errors_reach_the_customer_safely(stack: Stack) -> None:
    unknown = stack.get(
        HTTPS_PORT, f"/api/v1/conversations/{uuid.uuid4()}/messages",
        headers=bearer("cst_" + "x" * 43),
    )  # fmt: skip
    invalid = stack.send(HTTPS_PORT, "POST", "/api/v1/conversations", content=b"{not json")

    error = assert_safe(unknown)
    assert unknown.status_code == 404 and error["code"] == "SESSION_NOT_FOUND"
    assert error["request_id"] == unknown.headers["X-Request-ID"]  # traceable to the logs
    assert error["retryable"] is False
    assert invalid.status_code in (201, 400, 422)  # a body is ignored or refused, never a 500


def test_nginx_errors_follow_the_same_contract(stack: Stack) -> None:
    unknown_route = stack.get(HTTPS_PORT, "/api/v1/not-a-route")
    wrong_method = stack.send(HTTPS_PORT, "DELETE", "/api/v1/faqs")

    for response in (unknown_route, wrong_method):
        error = assert_safe(response)
        assert response.status_code == 404 and error["code"] == "NOT_FOUND"
        assert error["retryable"] is False
    assert unknown_route.headers["Server"] == "nginx"  # no version


def test_requests_for_other_host_names_are_refused(stack: Stack) -> None:
    with (
        httpx.Client(base_url=f"https://127.0.0.1:{HTTPS_PORT}", timeout=30,
                     verify=False,  # noqa: S501 - the refusal is the point, not the certificate
                     headers={"Host": "attacker.example"}) as client,
        pytest.raises(httpx.HTTPError),
    ):  # fmt: skip
        client.get("/")  # TLS with an unknown name: closed without a response (444)
