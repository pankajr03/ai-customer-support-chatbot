"""Error handling across both applications (Stage 9, Task 8), on the page.

Each case makes the real backend fail the way it would in production, then checks what
the customer sees: an understandable message, the right way forward (Try again, Start a
new conversation, the message back in the box), a request ID to quote, and nothing
technical. The mapping itself (backend status and code -> frontend error kind -> text and
control) is documented in frontend/README.md, "Loading and error states".
"""

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import openai
import pytest
from streamlit.testing.v1 import AppTest

from app.prompts.customer_support import SYSTEM_INSTRUCTIONS
from frontend.api_client import ConversationStatus, Decision, ErrorKind
from frontend.state import STATE_KEY, ChatState, Delivery
from tests.integration.conftest import (
    CONVERSATIONS,
    EXACT,
    FAKE_KEY,
    OUT_OF_SCOPE,
    GroundedLLM,
    Stack,
    page_text,
    say,
    unescape,
)

# Never on a customer's screen.
TECHNICAL = (
    "Traceback",
    "Exception",
    "Error:",
    "sqlalchemy",
    "sqlite",
    "SELECT ",
    "no such table",
    "conversation_messages",
    'File "',
    ".py",
    "cst_",
    FAKE_KEY.get_secret_value(),
    SYSTEM_INSTRUCTIONS[:60],
)


def state_of(page: AppTest) -> ChatState:
    return page.session_state[STATE_KEY]


def click(page: AppTest, label: str) -> AppTest:
    next(button for button in page.button if button.label == label).click().run()
    assert not page.exception
    return page


def assert_safe(page: AppTest) -> None:
    text = page_text(page)
    for leak in TECHNICAL:
        assert leak not in text, leak
    credentials = state_of(page).credentials
    if credentials is not None:
        assert credentials.access_token.get_secret_value() not in str(page._tree)


def reference(page: AppTest) -> str | None:
    return next((c.value for c in page.caption if c.value.startswith("Reference: ")), None)


def test_backend_offline_shows_a_connection_error_and_retries_on_request(
    stack: Stack, open_page: Callable[..., AppTest]
) -> None:
    stack.faults.plan = {CONVERSATIONS: ["refused", "refused"]}  # offline, twice
    page = open_page(stack)

    assert "We can't reach the support service right now" in page.error[0].value
    assert "Try again" in [b.label for b in page.button]
    assert_safe(page)
    click(page, "Try again")  # still offline: same message, nothing breaks
    assert "We can't reach the support service right now" in page.error[0].value
    click(page, "Try again")  # back online
    assert state_of(page).conversation_status is ConversationStatus.ACTIVE
    assert not page.error
    assert stack.faults.count("POST", CONVERSATIONS) == 3


def test_an_invalid_session_offers_a_new_conversation_with_the_message_kept(
    stack: Stack, open_page: Callable[..., AppTest]
) -> None:
    page = open_page(stack)
    old = state_of(page).credentials
    assert old is not None
    stack.api.delete_conversation(old)  # gone on the backend (another device, cleanup)

    say(page, EXACT)

    assert "This conversation is no longer available" in page.error[0].value
    assert state_of(page).credentials is None  # a useless token is dropped
    assert page.chat_input[0].disabled
    click(page, "Start a new conversation")
    assert state_of(page).credentials not in (None, old)
    assert page.chat_input[0].proto.value == EXACT  # ready to send again
    assert_safe(page)


def test_an_invalid_message_gets_a_useful_validation_message(
    make_stack: Callable[..., Stack], open_page: Callable[..., AppTest]
) -> None:
    page = say(open_page(make_stack(max_customer_message_characters=100)), "x" * 150)

    assert "Your message is too long (at most 100 characters)" in page.error[0].value
    assert page.chat_input[0].proto.value == "x" * 150  # nothing typed is lost
    assert reference(page) is not None  # the backend's 422 has a request ID
    assert_safe(page)


def test_a_message_already_processing_is_not_submitted_twice(
    stack: Stack, open_page: Callable[..., AppTest]
) -> None:
    page = open_page(stack)
    until = (datetime.now(UTC) + timedelta(minutes=2)).strftime("%Y-%m-%d %H:%M:%S")
    # Another request holds the conversation's processing lease (a slow LLM call).
    stack.sql(
        f"UPDATE conversation_sessions SET processing_key = 'other', "  # noqa: S608
        f"processing_until = '{until}'"
    )

    say(page, EXACT)

    assert "Your previous message is still being answered" in page.error[0].value
    assert "about" in page.error[0].value  # how long to wait (Retry-After)
    assert state_of(page).messages[-1].delivery is Delivery.PENDING
    assert stack.faults.count("POST", "/chat") == 1  # long Retry-After: no automatic loop
    stack.sql("UPDATE conversation_sessions SET processing_key = NULL, processing_until = NULL")
    click(page, "Try again")
    credentials = state_of(page).credentials
    assert credentials is not None
    assert stack.api.get_history(credentials).total_messages == 2  # answered once


def test_a_rate_limit_shows_a_temporary_restriction(
    make_stack: Callable[..., Stack], open_page: Callable[..., AppTest]
) -> None:
    page = open_page(make_stack(rate_limit_chat_messages_per_minute=1))
    say(page, EXACT)
    say(page, OUT_OF_SCOPE)

    error = page.error[0].value
    assert "You're sending messages a little too quickly" in error
    assert "You can try again in about" in error
    assert "Try again" in [b.label for b in page.button]
    assert reference(page) is not None
    assert_safe(page)


def test_llm_unavailable_shows_the_approved_answer_not_an_error(
    make_stack: Callable[..., Stack], open_page: Callable[..., AppTest], faq_service: Any
) -> None:
    timeout = openai.APITimeoutError(request=httpx.Request("POST", "https://api.openai.com"))
    page = say(open_page(make_stack(llm=GroundedLLM(error=timeout))), EXACT)

    assert not page.error
    assert unescape(page.chat_message[-1].markdown[0].value) == (
        faq_service.get_active_faq("faq_107").answer
    )
    assert "Answer from our help articles" in page_text(page)
    assert_safe(page)


def test_retrieval_unavailable_shows_the_backends_message_with_a_reference(
    make_stack: Callable[..., Stack], open_page: Callable[..., AppTest]
) -> None:
    def broken(name: str) -> Any:
        raise OSError("model files not found at C:\\models")

    stack = make_stack(loader=broken)
    page = say(open_page(stack), "Is there anything about orders?")

    state = state_of(page)
    reply = state.messages[-1]
    assert reply.decision is Decision.RETRIEVAL_UNAVAILABLE
    credentials = state.credentials
    assert credentials is not None
    stored = stack.api.get_history(credentials).messages[-1].content
    assert unescape(page.chat_message[-1].markdown[0].value) == stored  # the backend's text
    text = page_text(page)
    assert "briefly unavailable" in text and "Ask again" in text
    assert "Not found in our help articles" not in text  # not a no-match
    assert f"Reference: {reply.request_id}" in text and reply.request_id
    assert not page.error  # a normal reply, not a failed request
    assert "C:\\models" not in text
    assert_safe(page)


def test_a_legitimate_no_match_is_not_presented_as_a_failure(
    stack: Stack, open_page: Callable[..., AppTest]
) -> None:
    page = say(open_page(stack), OUT_OF_SCOPE)
    text = page_text(page)
    assert state_of(page).messages[-1].decision is Decision.NO_MATCH
    assert "Not found in our help articles" in text
    assert not page.error and "Try again" not in text and "Reference:" not in text
    assert "unavailable" not in text


def test_a_database_error_never_reaches_the_customer(
    stack: Stack, open_page: Callable[..., AppTest], caplog: pytest.LogCaptureFixture
) -> None:
    page = open_page(stack)
    stack.sql("DROP TABLE conversation_messages")

    with caplog.at_level(logging.DEBUG):
        say(page, EXACT)

    assert "The support service is temporarily unavailable" in page.error[0].value
    assert reference(page) is not None
    assert "Try again" in [b.label for b in page.button]
    assert_safe(page)
    # The diagnostic category goes to the log; SQL and its parameters (customer text) never.
    assert "event=api_error status=503 code=SERVICE_UNAVAILABLE" in caplog.text
    assert EXACT not in caplog.text and "no such table" not in caplog.text


def test_error_responses_carry_request_ids_but_no_internals(stack: Stack) -> None:
    credentials = stack.start()
    stack.sql("DROP TABLE conversation_messages")
    response = stack.raw_chat(credentials, str(credentials.session_id), {"message": EXACT})

    assert response.status_code == 503
    error = response.json()["error"]
    assert error["code"] == "SERVICE_UNAVAILABLE"
    assert error["request_id"] == response.headers["x-request-id"]
    for leak in TECHNICAL:
        assert leak not in response.text, leak


@pytest.mark.parametrize(
    ("kind", "control"),
    [
        (ErrorKind.BACKEND_UNREACHABLE, "Try again"),
        (ErrorKind.TIMEOUT, "Try again"),
        (ErrorKind.BUSY, "Try again"),
        (ErrorKind.RATE_LIMITED, "Try again"),
        (ErrorKind.SERVICE_UNAVAILABLE, "Try again"),
        (ErrorKind.SESSION_EXPIRED, "Start a new conversation"),
        (ErrorKind.SESSION_NOT_FOUND, "Start a new conversation"),
        (ErrorKind.SESSION_CLOSED, "Start a new conversation"),
    ],
)
def test_every_failure_offers_the_matching_way_forward(
    stack: Stack, open_page: Callable[..., AppTest], kind: ErrorKind, control: str
) -> None:
    from frontend.formatting import error_text
    from frontend.state import ErrorNotice

    page = open_page(stack)
    failing = stack.api.send_message

    def fail(*args: Any, **kwargs: Any) -> Any:
        from frontend.api_client import ApiError

        raise ApiError(kind, request_id="req-123")

    stack.api.send_message = fail  # type: ignore[method-assign]
    try:
        say(page, EXACT)
    finally:
        stack.api.send_message = failing  # type: ignore[method-assign]

    assert page.error[0].value == error_text(ErrorNotice(kind=kind, request_id="req-123"))
    assert control in [b.label for b in page.button]
    assert reference(page) == "Reference: req-123"
    assert_safe(page)
