"""One error shape for every endpoint (Stage 7, Task 8).

Every error is ``{"error": {"code", "message", "request_id"}}`` (plus ``details`` for
validation errors) with the right HTTP status; nothing internal is exposed, and each
error response logs its diagnostic category.
"""

import logging
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.models.errors import CODES_BY_STATUS, ErrorCode, ErrorResponse
from app.services.faq_service import FAQService
from tests.test_conversations_api import BASE, build_app, create

KEYS = {"code", "message", "request_id"}


@pytest.fixture
def app(make_settings: Callable[..., Settings], faq_service: FAQService) -> FastAPI:
    application = build_app(make_settings, faq_service)

    @application.get("/test/rate-limited")
    def rate_limited() -> None:
        raise HTTPException(status_code=429, detail="Too many requests")

    @application.get("/test/no-detail")
    def no_detail() -> None:
        raise HTTPException(status_code=418, detail={"internal": "not for clients"})

    @application.get("/test/crash")
    def crash() -> None:
        raise RuntimeError("secret detail in C:/srv/app.py with sk-test-key")

    return application


@pytest.fixture
def api(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


def error_of(response: Any, status: int, code: str) -> dict[str, Any]:
    """Check the envelope and return the error object."""
    assert response.status_code == status, response.text
    body = response.json()
    ErrorResponse.model_validate(body)
    assert set(body) == {"error"}
    error = body["error"]
    assert error["code"] == code
    assert error["request_id"] == response.headers["X-Request-ID"]
    assert error["message"] and error["message"].endswith(".")
    return error


# --- Every category -------------------------------------------------------------------


def test_validation_errors_keep_fields_and_rules_but_not_values(api: TestClient) -> None:
    personal = "jane.doe@example.com 4111 1111 1111 1111 "
    response = api.post("/api/v1/answer", json={"query": personal * 40, "extra": "x"})

    error = error_of(response, 422, "VALIDATION_ERROR")
    assert set(error) == KEYS | {"details"}
    by_type = {d["type"]: d for d in error["details"]}
    assert by_type["string_too_long"]["loc"] == ["body", "query"]
    assert by_type["string_too_long"]["ctx"] == {"max_length": 1000}
    assert by_type["extra_forbidden"]["loc"] == ["body", "extra"]
    assert all(set(d) <= {"loc", "type", "msg", "ctx"} for d in error["details"])
    assert "jane.doe" not in response.text and "4111" not in response.text


def test_query_parameter_errors_use_the_same_shape(api: TestClient) -> None:
    error = error_of(api.get("/api/v1/faqs", params={"limit": 0}), 422, "VALIDATION_ERROR")

    assert error["details"][0]["loc"] == ["query", "limit"]


def test_an_unreadable_body_is_400(api: TestClient) -> None:
    response = api.post(
        "/api/v1/answer", content=b'{"query": "\xff"}', headers={"Content-Type": "application/json"}
    )

    error_of(response, 400, "VALIDATION_ERROR")


def test_unknown_paths_methods_and_faqs(api: TestClient) -> None:
    error_of(api.get("/api/v1/nope"), 404, "NOT_FOUND")
    faq = error_of(api.get("/api/v1/faqs/faq_999"), 404, "NOT_FOUND")
    assert faq["message"] == "FAQ not found."
    wrong_method = api.get("/api/v1/answer")
    error_of(wrong_method, 405, "METHOD_NOT_ALLOWED")
    assert wrong_method.headers["Allow"] == "POST"


def test_rate_limited_is_reserved_for_429(api: TestClient) -> None:
    error_of(api.get("/test/rate-limited"), 429, "RATE_LIMITED")
    assert CODES_BY_STATUS[429] is ErrorCode.RATE_LIMITED


def test_unexpected_errors_are_500_without_internals(api: TestClient) -> None:
    response = api.get("/test/crash")

    error_of(response, 500, "INTERNAL_ERROR")
    for leak in ("secret", "C:/", "sk-test", "RuntimeError", "Traceback", ".py"):
        assert leak not in response.text


def test_conversation_errors(api: TestClient) -> None:
    sid, headers = create(api)
    token = headers["Authorization"].removeprefix("Bearer ")
    url = f"{BASE}/{sid}/chat"
    body = {"message": "What is your return policy?"}

    error_of(api.post(url, json=body), 401, "UNAUTHORIZED_SESSION")
    error_of(api.post(f"{url}?token={token}", json=body), 400, "VALIDATION_ERROR")
    wrong_token = api.post(url, json=body, headers={"Authorization": "Bearer cst_" + "x" * 43})
    unknown = api.post(f"{BASE}/00000000-0000-4000-8000-000000000000/chat", json=body,
                       headers=headers)  # fmt: skip
    assert {k: v for k, v in error_of(wrong_token, 404, "SESSION_NOT_FOUND").items()
            if k != "request_id"} == {
        k: v for k, v in error_of(unknown, 404, "SESSION_NOT_FOUND").items() if k != "request_id"
    }  # fmt: skip
    error_of(api.post(url, json={"message": "  "}, headers=headers), 422, "VALIDATION_ERROR")
    api.post(f"{BASE}/{sid}/close", headers=headers)
    error_of(api.post(url, json=body, headers=headers), 409, "SESSION_CLOSED")


def test_a_no_match_is_a_successful_200(api: TestClient) -> None:
    sid, headers = create(api)

    response = api.post(f"{BASE}/{sid}/chat", json={"message": "Do you sell submarines?"},
                        headers=headers)  # fmt: skip

    assert response.status_code == 200
    assert response.json()["decision"] in {"no_match", "ambiguous"}
    assert "error" not in response.json()


def test_every_code_has_one_meaning() -> None:
    assert len({code.value for code in ErrorCode}) == len(ErrorCode)
    assert {code.value for code in ErrorCode} >= {
        "VALIDATION_ERROR",
        "UNAUTHORIZED_SESSION",
        "SESSION_NOT_FOUND",
        "SESSION_EXPIRED",
        "SESSION_CLOSED",
        "DUPLICATE_MESSAGE_CONFLICT",
        "REQUEST_IN_PROGRESS",
        "RATE_LIMITED",
        "SERVICE_UNAVAILABLE",
        "INTERNAL_ERROR",
    }


# --- Logging --------------------------------------------------------------------------


def test_errors_log_their_category_but_no_values(
    api: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="app.errors")
    sid, headers = create(api)

    api.post(f"{BASE}/{sid}/chat", json={"message": "a\x00 jane@example.com"}, headers=headers)
    wrong = {"Authorization": "Bearer cst_secret_token_value_000000000000000000"}
    api.post(f"{BASE}/{sid}/chat", json={"message": "hi"}, headers=wrong)

    log = caplog.text
    assert "event=api_error status=422 code=VALIDATION_ERROR cause=RequestValidationError" in log
    assert (
        "event=api_error status=404 code=SESSION_NOT_FOUND cause=ConversationNotFoundError" in log
    )
    assert "jane@example.com" not in log and "cst_secret" not in log


# --- OpenAPI --------------------------------------------------------------------------


def test_openapi_documents_the_error_shape_everywhere(api: TestClient) -> None:
    schema = api.get("/openapi.json").json()

    assert "HTTPValidationError" not in schema["components"]["schemas"]
    for path, operations in schema["paths"].items():
        for method, operation in operations.items():
            for status, response in operation["responses"].items():
                if status.startswith(("4", "5")):
                    ref = response["content"]["application/json"]["schema"]["$ref"]
                    if (path, status) == ("/api/v1/ready", "503"):
                        # Readiness details plus the same "error" object as elsewhere.
                        assert ref.endswith("/ReadinessResponse")
                        error = schema["components"]["schemas"]["ReadinessResponse"]["properties"][
                            "error"
                        ]
                        assert {"$ref": "#/components/schemas/ErrorBody"} in error["anyOf"]
                        continue
                    assert ref.endswith("/ErrorResponse"), (method, path, status)


def test_http_errors_with_a_non_text_detail_get_a_generic_message(api: TestClient) -> None:
    response = api.get("/test/no-detail")
    error = error_of(response, 418, "VALIDATION_ERROR")

    assert "internal" not in response.text
    assert error["message"] == "The request is not valid. See details for the fields to fix."
