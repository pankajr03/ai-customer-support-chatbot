"""Security: authorization, isolation between sessions, and nothing secret leaking into
responses, logs, the page or the model's prompt."""

import logging
import uuid
from collections.abc import Callable

import pytest
from streamlit.testing.v1 import AppTest

from frontend.api_client import ApiError, Credentials, ErrorKind
from frontend.state import STATE_KEY
from tests.integration.conftest import (
    CONVERSATIONS,
    EXACT,
    FAKE_KEY,
    FOLLOW_UP,
    RETURNS,
    GroundedLLM,
    Stack,
    say,
)

WRONG = "cst_" + "w" * 43  # noqa: S105 - a fake token


def endpoints(sid: str) -> list[tuple[str, str]]:
    base = f"{CONVERSATIONS}/{sid}"
    return [
        ("GET", base),
        ("GET", f"{base}/messages"),
        ("POST", f"{base}/chat"),
        ("POST", f"{base}/close"),
        ("DELETE", base),
    ]


def test_every_conversation_endpoint_needs_authorization(stack: Stack) -> None:
    sid = str(stack.start().session_id)
    for method, url in endpoints(sid):
        response = stack.http.request(method, url, json={"message": "Hi"})
        assert response.status_code == 401, (method, url)
        assert response.json()["error"]["code"] == "UNAUTHORIZED_SESSION"


@pytest.mark.parametrize("token", [WRONG, "Bearer", "", "cst_" + "a" * 2000])
def test_an_invalid_token_looks_like_an_unknown_conversation(stack: Stack, token: str) -> None:
    sid = str(stack.start().session_id)
    for method, url in endpoints(sid):
        response = stack.http.request(
            method, url, json={"message": "Hi"}, headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code in (401, 404), (method, url)
        if response.status_code == 404:
            assert response.json()["error"]["code"] == "SESSION_NOT_FOUND"
    # Nothing changed: the real owner still has an active, empty conversation.


def test_unknown_and_foreign_sessions_are_indistinguishable(stack: Stack) -> None:
    alice, bob = stack.start(), stack.start()
    foreign = stack.http.get(f"{CONVERSATIONS}/{alice.session_id}", headers=bob.authorization())
    unknown = stack.http.get(f"{CONVERSATIONS}/{uuid.uuid4()}", headers=bob.authorization())
    assert foreign.status_code == unknown.status_code == 404
    assert foreign.json()["error"]["message"] == unknown.json()["error"]["message"]


def test_one_session_cannot_read_write_close_or_delete_another(stack: Stack) -> None:
    alice, bob = stack.start(), stack.start()
    stack.api.send_message(alice, EXACT, uuid.uuid4())
    as_bob = Credentials(session_id=alice.session_id, access_token=bob.access_token)

    for call in (
        lambda: stack.api.get_history(as_bob),
        lambda: stack.api.send_message(as_bob, RETURNS, uuid.uuid4()),
        lambda: stack.api.close_conversation(as_bob),
        lambda: stack.api.delete_conversation(as_bob),
    ):
        with pytest.raises(ApiError) as caught:
            call()
        assert caught.value.kind is ErrorKind.SESSION_NOT_FOUND
    assert stack.api.get_history(alice).total_messages == 2  # untouched
    assert stack.api.get_history(bob).total_messages == 0


def test_history_never_crosses_sessions_in_follow_ups(stack: Stack) -> None:
    alice, bob = stack.start(), stack.start()
    stack.api.send_message(alice, EXACT, uuid.uuid4())
    # Bob's follow-up has no previous turn of his own: Alice's question must not help.
    reply = stack.api.send_message(bob, FOLLOW_UP, uuid.uuid4())
    assert reply.decision == "no_match"


def test_the_token_is_returned_once_and_never_again(stack: Stack) -> None:
    created = stack.http.post(CONVERSATIONS).json()
    token = created["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    sid = created["session_id"]
    stack.http.post(f"{CONVERSATIONS}/{sid}/chat", json={"message": EXACT}, headers=headers)
    responses = [
        stack.http.get(f"{CONVERSATIONS}/{sid}", headers=headers),
        stack.http.get(f"{CONVERSATIONS}/{sid}/messages", headers=headers),
        stack.http.post(f"{CONVERSATIONS}/{sid}/chat", json={"message": EXACT}, headers=headers),
        stack.http.post(f"{CONVERSATIONS}/{sid}/close", headers=headers),
    ]
    for response in responses:
        assert token not in response.text and token not in str(response.headers)
    with stack.database.read() as session:
        from sqlalchemy import text

        stored = session.execute(text("SELECT access_token_hash FROM conversation_sessions"))
        assert all(token not in row[0] for row in stored)  # only a hash is stored


def test_tokens_keys_and_messages_stay_out_of_logs(
    make_stack: Callable[..., Stack], caplog: pytest.LogCaptureFixture
) -> None:
    private_message = "my account email is jane@example.com, reset my password"
    with caplog.at_level(logging.DEBUG):
        stack = make_stack(llm=GroundedLLM())
        credentials = stack.start()
        stack.api.send_message(credentials, private_message, uuid.uuid4())
        stack.api.get_history(credentials)
        stack.raw_chat(credentials, str(credentials.session_id), {"message": " "})  # a 422

    token = credentials.access_token.get_secret_value()
    for leak in (token, FAKE_KEY.get_secret_value(), "jane@example.com"):
        assert leak not in caplog.text


def test_the_page_never_shows_the_token_or_the_prompt(
    make_stack: Callable[..., Stack], open_page: Callable[..., AppTest]
) -> None:
    from app.prompts.customer_support import SYSTEM_INSTRUCTIONS

    stack = make_stack(llm=GroundedLLM())
    page = say(open_page(stack), EXACT)
    credentials = page.session_state[STATE_KEY].credentials
    assert credentials is not None
    everything = str(page._tree)
    assert credentials.access_token.get_secret_value() not in everything
    assert "**********" not in everything  # not even masked
    assert SYSTEM_INSTRUCTIONS[:80] not in everything
    assert FAKE_KEY.get_secret_value() not in everything
    assert credentials.access_token.get_secret_value() not in repr(page.session_state[STATE_KEY])


def test_error_responses_hold_no_sensitive_information(stack: Stack) -> None:
    credentials = stack.start()
    sid = str(credentials.session_id)
    responses = [
        stack.http.get(f"{CONVERSATIONS}/{sid}/messages?token={WRONG}"),  # token in URL
        stack.raw_chat(credentials, sid, {"message": "a\x00b"}),  # control character
        stack.raw_chat(credentials, sid, ["not", "an", "object"]),
        stack.http.post(
            f"{CONVERSATIONS}/{sid}/chat",
            content=b"\xff\xfe not utf-8",
            headers={**credentials.authorization(), "Content-Type": "application/json"},
        ),
    ]
    token = credentials.access_token.get_secret_value()
    for response in responses:
        assert 400 <= response.status_code < 500
        body = response.text
        for leak in (token, WRONG, "Traceback", "pydantic", 'File "', "a\\u0000b", "not utf-8"):
            assert leak not in body, (response.status_code, leak)
