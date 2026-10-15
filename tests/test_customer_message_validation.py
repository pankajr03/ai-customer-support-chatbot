"""Validation of incoming customer chat messages (Stage 7, Task 4).

Rules: JSON object bodies only; known fields only; the message must not be empty,
whitespace-only or contain control characters; whitespace around it is removed and
nothing else is changed; the length limit is configurable
(MAX_CUSTOMER_MESSAGE_CHARACTERS); client_message_id must be a canonical UUID. Every
invalid request is a 422 and stores nothing.
"""

from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core.config import (
    DEFAULT_MAX_CUSTOMER_MESSAGE_CHARACTERS,
    MAX_CUSTOMER_MESSAGE_CEILING,
    Settings,
)
from app.models.chat import CustomerChatRequest, strip_message
from app.models.conversation import MAX_ASSISTANT_MESSAGE_LENGTH, MAX_USER_MESSAGE_LENGTH
from app.services.faq_service import FAQService
from tests.test_conversations_api import BASE, build_app, create

VALID_ID = "4c981eb1-7847-41c0-bf6b-0a0e1b9d6d91"


def message(value: Any) -> str:
    return CustomerChatRequest.model_validate({"message": value}).message


def error_types(body: dict[str, Any]) -> set[str]:
    try:
        CustomerChatRequest.model_validate(body)
    except ValidationError as exc:
        return {error["type"] for error in exc.errors()}
    return set()


# --- Message text ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "",
        " ",
        "   \t\n\r\n  ",
        " 　 ",  # no-break, ideographic and em spaces
        "​﻿⁠",  # zero-width space, byte-order mark, word joiner
        " ​ \n ",
    ],
)
def test_empty_and_whitespace_only_messages_are_rejected(value: str) -> None:
    assert error_types({"message": value}) == {"blank_message"}


def test_missing_and_null_messages_are_rejected() -> None:
    assert error_types({}) == {"missing"}
    assert error_types({"message": None}) == {"string_type"}


@pytest.mark.parametrize("value", [42, 4.2, True, ["hi"], {"text": "hi"}])
def test_non_text_messages_are_rejected(value: Any) -> None:
    assert error_types({"message": value}) == {"string_type"}


def test_only_surrounding_whitespace_is_removed() -> None:
    assert message("  \n\tWhere is my order?  \r\n") == "Where is my order?"
    assert message("​﻿Hello​") == "Hello"  # invisible edges too
    assert strip_message(42) == 42  # left for the type check


@pytest.mark.parametrize(
    "value",
    [
        "Order #A-1234: it's late!!! (3rd time...)",
        "My items:\n- blue shirt, size M\n- 2 x socks\n\nThanks",
        "Two  spaces   kept",
        "Tab\tseparated",
        "¿Puedo devolver mi pedido? Gracias 🙂",
        "注文をキャンセルできますか？",
        "هل يمكنني إرجاع طلبي؟",
        "Ünïcödé café naïve façade",
        "é (decomposed) stays decomposed",  # no Unicode normalisation
        "Family: 👩‍👩‍👧 and a coder 👩‍💻",  # joiners inside are kept
        "Price was €49,99 / £42 / $50 — 10% off?",
        "<b>not html</b> & 'quotes' \"double\" `code` \\ backslash",
    ],
)
def test_punctuation_unicode_and_inner_whitespace_are_preserved(value: str) -> None:
    assert message(value) == value
    assert message(f"  {value}  ") == value


@pytest.mark.parametrize(
    "value",
    [
        "a\x00b",  # NUL
        "colour\x1b[31mred",  # terminal escape
        "bell\x07",
        "delete\x7fme",
        "next\x85line inside",  # C1 control
    ],
)
def test_control_characters_are_rejected(value: str) -> None:
    assert error_types({"message": value}) == {"invalid_characters"}


def test_invalid_unicode_is_rejected() -> None:
    # An unpaired surrogate (valid JSON: "\ud800") is not text and cannot be stored.
    assert error_types({"message": "lone \ud800 surrogate"}) == {"string_unicode"}


def test_the_storage_ceiling_is_enforced_by_the_model() -> None:
    assert len(message("x" * MAX_USER_MESSAGE_LENGTH)) == MAX_USER_MESSAGE_LENGTH
    assert error_types({"message": "x" * (MAX_USER_MESSAGE_LENGTH + 1)}) == {"string_too_long"}
    # Surrounding whitespace does not count.
    assert len(message(f"  {'x' * MAX_USER_MESSAGE_LENGTH}  ")) == MAX_USER_MESSAGE_LENGTH


def test_limits_agree_with_storage() -> None:
    assert MAX_CUSTOMER_MESSAGE_CEILING == MAX_USER_MESSAGE_LENGTH
    assert MAX_USER_MESSAGE_LENGTH <= MAX_ASSISTANT_MESSAGE_LENGTH  # the database check


# --- client_message_id ----------------------------------------------------------------


@pytest.mark.parametrize("value", [VALID_ID, VALID_ID.upper()])
def test_canonical_uuids_are_accepted(value: str) -> None:
    request = CustomerChatRequest.model_validate({"message": "hi", "client_message_id": value})

    assert str(request.client_message_id) == VALID_ID


@pytest.mark.parametrize(
    "value",
    [
        "",
        "not-a-uuid",
        VALID_ID.replace("-", ""),  # no hyphens
        "{" + VALID_ID + "}",  # braces
        "urn:uuid:" + VALID_ID,
        f" {VALID_ID} ",
        VALID_ID[:-1],
        VALID_ID + "0",
        "4c981eb1-7847-41c0-bf6b-0a0e1b9d6d9g",  # not hex
    ],
)
def test_malformed_client_message_ids_are_rejected(value: str) -> None:
    assert error_types({"message": "hi", "client_message_id": value}) == {"uuid_format"}


@pytest.mark.parametrize(
    "value", ["00000000-0000-0000-0000-000000000000", "ffffffff-ffff-ffff-ffff-ffffffffffff"]
)
def test_reserved_uuids_are_rejected(value: str) -> None:
    assert error_types({"message": "hi", "client_message_id": value}) == {"uuid_reserved"}


@pytest.mark.parametrize("value", [42, ["x"], {"id": VALID_ID}, True])
def test_non_string_client_message_ids_are_rejected(value: Any) -> None:
    assert error_types({"message": "hi", "client_message_id": value})


# --- Fields ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field",
    ["role", "history", "session_id", "access_token", "token", "prompt", "system", "Message"],
)
def test_unsupported_fields_are_rejected(field: str) -> None:
    assert error_types({"message": "hi", field: "x"}) == {"extra_forbidden"}


def test_unknown_categories_are_rejected() -> None:
    assert error_types({"message": "hi", "category": "not-a-category"})


# --- Settings -------------------------------------------------------------------------


def test_message_limit_default_and_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    assert Settings(_env_file=None).max_customer_message_characters == 4000
    assert DEFAULT_MAX_CUSTOMER_MESSAGE_CHARACTERS == 4000

    monkeypatch.setenv("MAX_CUSTOMER_MESSAGE_CHARACTERS", "1500")
    settings = Settings(_env_file=None)

    assert settings.max_customer_message_characters == 1500
    assert settings.safe_summary()["max_customer_message_characters"] == 1500


@pytest.mark.parametrize("value", ["99", "10001", "lots"])
def test_message_limit_setting_is_validated(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("MAX_CUSTOMER_MESSAGE_CHARACTERS", value)

    with pytest.raises(ValidationError, match="max_customer_message_characters"):
        Settings(_env_file=None)


def test_the_body_limit_must_fit_the_longest_message() -> None:
    with pytest.raises(ValidationError, match="MAX_REQUEST_BODY_BYTES"):
        Settings(_env_file=None, max_customer_message_characters=10_000)  # 41 KB > 32 KB

    ok = Settings(
        _env_file=None, max_customer_message_characters=10_000, max_request_body_bytes=41_024
    )
    assert ok.max_customer_message_characters == 10_000


# --- HTTP -----------------------------------------------------------------------------


@pytest.fixture
def api(make_settings: Callable[..., Settings], faq_service: FAQService) -> Iterator[TestClient]:
    app = build_app(make_settings, faq_service, max_customer_message_characters=200)
    with TestClient(app) as client:
        yield client


def stored_count(api: TestClient, sid: str, headers: dict[str, str]) -> int:
    return api.get(f"{BASE}/{sid}/messages", headers=headers).json()["total_messages"]


def post_raw(api: TestClient, sid: str, headers: dict[str, str], **kwargs: Any) -> Any:
    return api.post(f"{BASE}/{sid}/chat", headers={**headers, **kwargs.pop("extra", {})}, **kwargs)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"content": b'{"message": "hi"}', "extra": {"Content-Type": "text/plain"}},
        {"content": b'{"message": "hi"}'},  # no Content-Type
        {"data": {"message": "hi"}},  # a form
        {"content": b'{"message": ', "extra": {"Content-Type": "application/json"}},
        {"content": b"", "extra": {"Content-Type": "application/json"}},
        {"content": b"null", "extra": {"Content-Type": "application/json"}},
        {"json": ["hi"]},
        {"json": "hi"},
    ],
)
def test_only_json_objects_are_accepted(api: TestClient, kwargs: dict[str, Any]) -> None:
    sid, headers = create(api)

    response = post_raw(api, sid, headers, **kwargs)

    assert response.status_code == 422
    assert isinstance(response.json()["detail"], list)
    assert stored_count(api, sid, headers) == 0


def test_a_body_that_is_not_utf8_is_a_400_and_stores_nothing(api: TestClient) -> None:
    """Not readable as text at all, so FastAPI answers 400 before any field is checked."""
    sid, headers = create(api)
    extra = {"Content-Type": "application/json"}

    response = post_raw(api, sid, headers, content=b'{"message": "\xff"}', extra=extra)

    assert response.status_code == 400
    assert stored_count(api, sid, headers) == 0


def test_lone_surrogate_escapes_in_json_are_422(api: TestClient) -> None:
    sid, headers = create(api)
    extra = {"Content-Type": "application/json"}

    response = post_raw(api, sid, headers, content=b'{"message": "a \\ud800 b"}', extra=extra)

    assert response.status_code == 422
    assert stored_count(api, sid, headers) == 0


def test_json_with_a_charset_is_accepted(api: TestClient) -> None:
    sid, headers = create(api)
    extra = {"Content-Type": "application/json; charset=utf-8"}

    response = post_raw(api, sid, headers, content=b'{"message": "hi"}', extra=extra)

    assert response.status_code == 200


def test_the_configured_limit_is_a_standard_422(api: TestClient) -> None:
    sid, headers = create(api)

    at_limit = api.post(f"{BASE}/{sid}/chat", json={"message": "y" * 200}, headers=headers)
    padded = api.post(f"{BASE}/{sid}/chat", json={"message": f"  {'y' * 200}\n"}, headers=headers)
    too_long = api.post(f"{BASE}/{sid}/chat", json={"message": "z" * 201}, headers=headers)

    assert at_limit.status_code == 200 and padded.status_code == 200
    assert too_long.status_code == 422
    assert too_long.json() == {
        "detail": [
            {
                "type": "string_too_long",
                "loc": ["body", "message"],
                "msg": "String should have at most 200 characters",
                "ctx": {"max_length": 200},
            }
        ]
    }
    assert "zzz" not in too_long.text  # the message is not echoed back
    assert too_long.headers["Cache-Control"] == "no-store"
    assert stored_count(api, sid, headers) == 4  # only the two accepted exchanges


@pytest.mark.parametrize(
    "body",
    [
        {"message": "   "},
        {"message": "a\x00b"},
        {"message": "hi", "client_message_id": "not-a-uuid"},
        {"message": "hi", "role": "system"},
    ],
)
def test_invalid_requests_are_422_and_store_nothing(api: TestClient, body: dict) -> None:
    sid, headers = create(api)

    assert api.post(f"{BASE}/{sid}/chat", json=body, headers=headers).status_code == 422
    assert stored_count(api, sid, headers) == 0


def test_the_message_is_stored_exactly_as_sent_apart_from_surrounding_whitespace(
    api: TestClient,
) -> None:
    sid, headers = create(api)
    text = "¿Mi pedido?\n\n- 2 × camisetas 👕\n- Ref: A-1234"

    response = api.post(f"{BASE}/{sid}/chat", json={"message": f"\n  {text}  \n"}, headers=headers)

    assert response.status_code == 200
    assert response.json()["user_message"]["content"] == text
    history = api.get(f"{BASE}/{sid}/messages", headers=headers).json()
    assert history["messages"][0]["content"] == text


def test_openapi_documents_the_message_rules(api: TestClient) -> None:
    schema = api.get("/openapi.json").json()["components"]["schemas"]["CustomerChatRequest"]
    message_schema = schema["properties"]["message"]

    assert message_schema["maxLength"] == MAX_USER_MESSAGE_LENGTH
    assert "MAX_CUSTOMER_MESSAGE_CHARACTERS" in message_schema["description"]
    assert schema["additionalProperties"] is False
