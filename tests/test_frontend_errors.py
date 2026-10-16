"""Loading and error states of the Streamlit page (Stage 8, Task 13).

For every failure a customer can meet, the page must say what happened and what to do,
keep what the customer typed, never send a message twice, never show a raw exception,
and stay usable. The matrix below covers each failure while sending a message; the page
tests drive the real page (AppTest) against a scripted backend.
"""

import json
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from streamlit.testing.v1 import AppTest

from frontend import actions, resources
from frontend.api_client import (
    ApiError,
    BackendStatus,
    ChatApiClient,
    ConversationStatus,
    ErrorKind,
)
from frontend.components import chat
from frontend.config import FrontendSettings
from frontend.formatting import DECISION_HINTS, error_text
from frontend.state import STATE_KEY, ChatState, Delivery, ErrorNotice, RetryAction
from tests.test_frontend_state import FakeClient, opened, send

APP = Path(__file__).resolve().parents[1] / "frontend" / "app.py"
CONVERSATIONS = "/api/v1/conversations"

# kind -> (message kept for "Try again", conversation ended, text points to)
SEND_FAILURES: dict[ErrorKind, tuple[bool, bool, str]] = {
    ErrorKind.BACKEND_UNREACHABLE: (True, False, "Try again"),  # backend offline
    ErrorKind.TIMEOUT: (True, False, "Try again"),  # network timeout
    ErrorKind.NETWORK_ERROR: (True, False, "Try again"),
    ErrorKind.SESSION_NOT_FOUND: (False, True, "Start a new conversation"),  # invalid
    ErrorKind.UNAUTHORIZED: (False, True, "Start a new conversation"),  # invalid
    ErrorKind.FORBIDDEN: (False, True, "Start a new conversation"),  # invalid
    ErrorKind.SESSION_EXPIRED: (False, True, "Start a new conversation"),  # expired
    ErrorKind.SESSION_CLOSED: (False, True, "Start a new conversation"),  # closed
    ErrorKind.MESSAGE_CONFLICT: (False, False, "message box"),  # duplicate conflict
    ErrorKind.BUSY: (True, False, "Try again"),  # request already processing
    ErrorKind.RATE_LIMITED: (True, False, "Try again"),  # rate limiting
    ErrorKind.INVALID_MESSAGE: (False, False, "send it again"),  # validation error
    ErrorKind.SERVICE_UNAVAILABLE: (True, False, "Try again"),  # temporary failure
    ErrorKind.SERVER_ERROR: (False, False, "message box"),
    ErrorKind.INVALID_RESPONSE: (True, False, "Try again"),
    ErrorKind.CLIENT_ERROR: (False, False, "try again later"),
}


def test_every_failure_kind_is_covered() -> None:
    assert set(SEND_FAILURES) == set(ErrorKind)


@pytest.mark.parametrize(("kind", "expected"), SEND_FAILURES.items())
def test_failures_while_sending(kind: ErrorKind, expected: tuple[bool, bool, str]) -> None:
    kept, ended, action = expected
    client = FakeClient(send=[ApiError(kind)])
    state = opened(client)
    send(state, client, "My question")

    assert action.casefold() in error_text(state.last_error).casefold()  # type: ignore[arg-type]
    assert state.has_ended is ended
    if kept:  # still on the page, "Try again" resends it with the same ID
        assert state.can_retry and state.messages[-1].delivery is Delivery.PENDING
        assert state.retry_pending()
        actions.send_pending(state, client)  # type: ignore[arg-type]
        first, retry = (args[2] for name, args in client.calls if name == "send")
        assert first == retry
        assert [m.role for m in state.messages] == ["user", "assistant"]
    else:  # nothing was stored: the text goes back into the input
        assert not state.can_retry and state.messages == []
        assert state.take_draft() == "My question"
        assert state.can_send is not ended  # still usable unless the conversation ended


@pytest.mark.parametrize(
    "kind", [ErrorKind.SESSION_EXPIRED, ErrorKind.UNAUTHORIZED, ErrorKind.SESSION_CLOSED]
)
def test_a_new_conversation_brings_the_unsent_message_back(kind: ErrorKind) -> None:
    client = FakeClient(send=[ApiError(kind)])
    state = opened(client)
    send(state, client, "Where is my parcel?")
    state.take_draft()  # shown in the (disabled) input
    assert actions.start_new_conversation(state, client)  # type: ignore[arg-type]
    assert state.conversation_status is ConversationStatus.ACTIVE and state.can_send
    assert state.take_draft() == "Where is my parcel?"  # ready to send again


def test_an_unsent_message_is_forgotten_once_a_new_one_is_sent() -> None:
    client = FakeClient(send=[ApiError(ErrorKind.SESSION_EXPIRED)])
    state = opened(client)
    send(state, client, "Old")
    actions.start_new_conversation(state, client)  # type: ignore[arg-type]
    send(state, client, "New")
    actions.start_new_conversation(state, client)  # type: ignore[arg-type]
    assert state.take_draft() is None


@pytest.mark.parametrize(
    ("action", "method", "run"),
    [
        (RetryAction.NEW_CONVERSATION, "create", actions.start_new_conversation),
        (RetryAction.CLOSE, "close", actions.close_conversation),
        (RetryAction.DELETE, "delete", actions.delete_conversation),
        (RetryAction.RELOAD, "history", actions.reload_history),
    ],
)
def test_a_temporary_sidebar_failure_can_be_tried_again(
    action: RetryAction, method: str, run: Callable[..., Any]
) -> None:
    client = FakeClient()
    state = opened(client)
    send(state, client)
    client.script[method] = [ApiError(ErrorKind.SERVICE_UNAVAILABLE)]
    run(state, client)
    assert state.action_to_retry is action
    assert state.can_send  # the conversation stays usable meanwhile
    if method == "history":
        client.script["history"] = [_history(state)]
    calls_before = len(client.calls)
    assert actions.retry_last_action(state, client) is action  # type: ignore[arg-type]
    assert client.names()[calls_before] == method  # the failed call is made again
    assert state.action_to_retry is None and state.last_error is None


def test_a_definite_sidebar_failure_offers_no_retry() -> None:
    client = FakeClient(close=[ApiError(ErrorKind.CLIENT_ERROR)])
    state = opened(client)
    actions.close_conversation(state, client)  # type: ignore[arg-type]
    assert state.last_error == ErrorNotice(kind=ErrorKind.CLIENT_ERROR)
    assert state.action_to_retry is None


def test_typing_after_a_failed_start_clears_the_start_retry() -> None:
    client = FakeClient(create=[ApiError(ErrorKind.BACKEND_UNREACHABLE)])
    state = opened(client)
    assert state.can_retry_start
    state.queue_message("Hi", max_chars=4000)
    assert not state.can_retry_start  # sending creates the conversation instead


def test_no_match_wording_suggests_what_to_do() -> None:
    from frontend.api_client import Decision

    hint = DECISION_HINTS[Decision.NO_MATCH]
    assert "rephras" in hint
    # Contacting support is the backend's own no-match message (and the optional link):
    # the note doesn't repeat it, or contradict a differently worded configured message.
    assert "support" not in hint


def _history(state: ChatState) -> Any:
    from frontend.api_client import ConversationHistory

    assert state.credentials is not None
    return ConversationHistory(
        session_id=state.credentials.session_id,
        status=ConversationStatus.ACTIVE,
        messages=[],
        total_messages=0,
    )


# --- The page -------------------------------------------------------------------------


READY = {
    "status": "ready",
    "service": "x",
    "checks": {
        "knowledge_base": "ok",
        "conversation_storage": "ok",
        "semantic_search": "ready",
        "llm": "disabled",
    },
}


class Backend:
    """A scripted backend: ``chat`` holds the response for each chat request (a dict for
    a reply, an int for an error status, an exception to raise)."""

    def __init__(self, *chat: Any) -> None:
        self.chat = list(chat)
        self.chat_bodies: list[dict[str, Any]] = []
        self.sessions = 0

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/ready":
            return httpx.Response(200, json=READY)
        if request.url.path == CONVERSATIONS:
            self.sessions += 1
            self.session_id = str(uuid.uuid4())
            return httpx.Response(201, json={
                "session_id": self.session_id, "access_token": "cst_x", "token_type": "Bearer",
                "status": "active", "created_at": "2026-10-15T09:30:00Z", "expires_at": None,
            })  # fmt: skip
        body = json.loads(request.content)
        self.chat_bodies.append(body)
        step = self.chat.pop(0)
        if isinstance(step, Exception):
            raise step
        if isinstance(step, int):
            codes = {401: "UNAUTHORIZED_SESSION", 429: "RATE_LIMITED", 410: "SESSION_EXPIRED"}
            return httpx.Response(
                step,
                json={"error": {"code": codes.get(step, "X"), "message": "m", "request_id": "r9"}},
                headers={"Retry-After": "30"} if step == 429 else {},
            )
        return httpx.Response(200, json={
            "session_id": self.session_id, "message_id": str(uuid.uuid4()),
            "client_message_id": body["client_message_id"], "request_id": "r",
            "decision": "answered", "generation_method": "approved_faq",
            "sources": [{"faq_id": "faq_027", "question": "How do I reset my password?"}],
            "created_at": "2026-10-15T09:31:00Z", **step,
        })  # fmt: skip

    def client(self) -> ChatApiClient:
        transport = httpx.MockTransport(self.handle)
        http = httpx.Client(base_url="http://backend.test", transport=transport)
        return ChatApiClient(http_client=http, sleep=lambda seconds: None)


ANSWER = {"answer": "Select Forgot Password on the sign-in page."}


@pytest.fixture
def open_page(monkeypatch: pytest.MonkeyPatch) -> Callable[[Backend], AppTest]:
    def _open(backend: Backend) -> AppTest:
        client = backend.client()
        healthy = lambda _client: BackendStatus.ONLINE  # noqa: E731
        healthy.clear = lambda: None  # type: ignore[attr-defined]
        monkeypatch.setattr(resources, "get_api_client", lambda: client)
        monkeypatch.setattr(resources, "backend_status", healthy)
        monkeypatch.setattr(resources, "get_settings", lambda: FrontendSettings(_env_file=None))
        page = AppTest.from_file(str(APP), default_timeout=30)
        page.run()
        assert not page.exception
        return page

    return _open


def say(page: AppTest, text: str) -> AppTest:
    page.chat_input[0].set_value(text).run()
    assert not page.exception
    return page


def click(page: AppTest, label: str) -> AppTest:
    next(button for button in page.button if button.label == label).click().run()
    assert not page.exception
    return page


def state_of(page: AppTest) -> ChatState:
    return page.session_state[STATE_KEY]


def input_text(page: AppTest) -> str:
    """The text the page pushed into the chat input on its last run."""
    proto = page.chat_input[0].proto
    return proto.value if proto.set_value else ""


def test_an_invalid_session_offers_a_new_one_with_the_message_kept(open_page: Callable) -> None:
    backend = Backend(401, ANSWER)
    page = say(open_page(backend), "Reset my password")
    assert "can no longer be used" in page.error[0].value
    assert page.chat_input[0].disabled  # no more messages in this conversation
    click(page, "Start a new conversation")
    assert backend.sessions == 2 and state_of(page).can_send
    assert input_text(page) == "Reset my password"  # ready to send again
    say(page, "Reset my password")
    assert state_of(page).messages[-1].content == ANSWER["answer"]


def test_rate_limiting_keeps_the_message_and_retries_it_once(open_page: Callable) -> None:
    backend = Backend(429, ANSWER)
    page = say(open_page(backend), "Hello")
    assert "about 30 s" in page.error[0].value
    assert "Not answered yet" in "\n".join(c.value for c in page.caption)
    assert not page.chat_input[0].disabled  # still usable
    click(page, "Try again")
    first, retry = backend.chat_bodies
    assert first == retry  # same text and client_message_id
    assert [m.role for m in state_of(page).messages] == ["user", "assistant"]
    assert not page.error


def test_an_unreadable_reply_is_retried_with_the_same_id(open_page: Callable) -> None:
    backend = Backend({"decision": "made_up"}, ANSWER)
    page = say(open_page(backend), "Hello")
    assert "unexpected reply" in page.error[0].value
    click(page, "Try again")
    assert backend.chat_bodies[0] == backend.chat_bodies[1]
    assert not page.error


def test_a_timeout_shows_a_retry_not_an_exception(open_page: Callable) -> None:
    backend = Backend(httpx.ReadTimeout("timed out after 60 s"), ANSWER)
    page = say(open_page(backend), "Hello")
    assert "taking longer than expected" in page.error[0].value
    assert "timed out after" not in str(page.error[0].proto)
    click(page, "Try again")
    assert state_of(page).messages[-1].content == ANSWER["answer"]


def test_an_llm_outage_is_invisible_to_the_customer(open_page: Callable) -> None:
    # With the LLM unavailable the backend answers with the approved FAQ text.
    page = say(open_page(Backend(ANSWER)), "Reset my password")
    assert not page.error
    assert "Answer from our help articles" in "\n".join(c.value for c in page.caption)


def test_an_unexpected_bug_shows_a_friendly_message_and_a_way_out(
    open_page: Callable, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("secret internal detail in C:\\app\\frontend\\x.py")

    monkeypatch.setattr(chat, "render_messages", broken)
    page = open_page(Backend())
    assert not page.exception  # caught by the page, not shown by Streamlit
    assert "Something went wrong on this page" in page.error[0].value
    assert "secret internal detail" not in str([e.proto for e in page.error])
    monkeypatch.undo()
    page_state_before = state_of(page)
    click(page, "Start over")
    assert state_of(page) is not page_state_before  # a fresh state
    assert state_of(page).conversation_status is ConversationStatus.ACTIVE


def test_the_service_badge_is_rechecked_while_the_backend_is_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    http = httpx.Client(base_url="http://backend.test", transport=httpx.MockTransport(refuse))
    client = ChatApiClient(http_client=http)
    cleared: list[bool] = []
    healthy = lambda _client: BackendStatus.UNAVAILABLE  # noqa: E731
    healthy.clear = lambda: cleared.append(True)  # type: ignore[attr-defined]
    monkeypatch.setattr(resources, "get_api_client", lambda: client)
    monkeypatch.setattr(resources, "backend_status", healthy)
    monkeypatch.setattr(resources, "get_settings", lambda: FrontendSettings(_env_file=None))
    page = AppTest.from_file(str(APP), default_timeout=30)
    page.run()  # offline from the start: the start fails
    page.run()
    assert cleared  # the cached health result is not trusted while the error shows
