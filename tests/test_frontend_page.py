"""The Streamlit page with mocked HTTP responses (Stage 9, Task 12).

Runs ``frontend/app.py`` headless with Streamlit's ``AppTest``; the API client talks to
an ``httpx.MockTransport`` that plays the backend, so each state (loading, errors,
expiry, duplicates) can be set up exactly. Whole journeys against the real backend are
in test_frontend_lifecycle.py and tests/integration/.

**What AppTest cannot show** (see frontend/README.md, "Tests"): it runs the script and
records the elements it produces; there is no browser. So the spinner animation,
the browser disabling the input on submit (``submit_mode="disable"``), CSS and layout,
clicks (callbacks are invoked directly), page reloads and WebSocket reconnects are not
exercised here. The loading state is checked by stopping the run while the request is
in flight (``st.stop()`` from inside the mocked backend), which leaves exactly the
elements the browser shows while it waits.
"""

import json
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest
from streamlit.testing.v1.errors import AppTestError

from frontend import resources
from frontend.api_client import BackendStatus, ChatApiClient, ConversationStatus
from frontend.config import FrontendSettings
from frontend.state import STATE_KEY, ChatState, Delivery, RequestStatus
from tests.test_frontend_app import APP

SESSION = str(uuid.uuid4())
# Relative to now: a fixed timestamp would make every conversation expired once it passed.
EXPIRES = (datetime.now(UTC) + timedelta(hours=1)).replace(microsecond=0)
CREATED = {
    "session_id": SESSION,
    "access_token": "cst_" + "t" * 43,
    "token_type": "Bearer",
    "status": "active",
    "created_at": "2026-10-16T09:30:00Z",
    "expires_at": EXPIRES.isoformat().replace("+00:00", "Z"),
}
SOURCE_RETURNS = {"faq_id": "faq_018", "question": "What is your return policy?"}
SOURCE_RETURN_SHIPPING = {"faq_id": "faq_020", "question": "Do I have to pay for return shipping?"}


def reply(body: dict[str, Any], **fields: Any) -> dict[str, Any]:
    return {
        "session_id": SESSION,
        "message_id": str(uuid.uuid4()),
        "client_message_id": body["client_message_id"],
        "request_id": "req-" + uuid.uuid4().hex[:8],
        "decision": "answered",
        "answer": "An answer.",
        "generation_method": "approved_faq",
        "sources": [],
        "created_at": "2026-10-16T09:31:00Z",
        **fields,
    }


class Backend:
    """Plays the backend. ``chat`` holds one step per chat request: a dict of reply
    fields, an ``httpx.Response``, or a callable run while the request is in flight."""

    def __init__(self, *chat: Any) -> None:
        self.chat = list(chat)
        self.requests: list[tuple[str, str, dict[str, Any] | None]] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.requests.append((request.method, request.url.path, body))
        if request.url.path.endswith("/conversations"):
            return httpx.Response(201, json=CREATED)
        if request.url.path.endswith("/chat"):
            step = self.chat.pop(0)
            if callable(step):
                step = step()
            if isinstance(step, httpx.Response):
                return step
            return httpx.Response(200, json=reply(body or {}, **step))
        raise AssertionError(f"unexpected {request.method} {request.url.path}")

    def count(self, suffix: str) -> int:
        return sum(1 for _, path, _ in self.requests if path.endswith(suffix))

    def chat_ids(self) -> list[str]:
        return [b["client_message_id"] for _, p, b in self.requests if p.endswith("/chat") and b]


@pytest.fixture
def open_page(monkeypatch: pytest.MonkeyPatch) -> Callable[[Backend], AppTest]:
    def _open(backend: Backend) -> AppTest:
        transport = httpx.MockTransport(backend.handle)
        client = ChatApiClient(
            http_client=httpx.Client(base_url="http://backend.test", transport=transport),
            sleep=lambda seconds: None,
        )
        status = lambda _client: BackendStatus.ONLINE  # noqa: E731
        status.clear = lambda: None  # type: ignore[attr-defined]
        monkeypatch.setattr(resources, "get_api_client", lambda: client)
        monkeypatch.setattr(resources, "backend_status", status)
        monkeypatch.setattr(resources, "get_settings", lambda: FrontendSettings(_env_file=None))
        page = AppTest.from_file(str(APP), default_timeout=30)
        page.run()
        assert not page.exception
        return page

    return _open


def state_of(page: AppTest) -> ChatState:
    return page.session_state[STATE_KEY]


def say(page: AppTest, text: str) -> AppTest:
    page.chat_input[0].set_value(text).run()
    assert not page.exception
    return page


def roles(page: AppTest) -> list[str]:
    return [message.name for message in page.chat_message][1:]  # after the welcome


def texts(page: AppTest) -> list[str]:
    return [message.markdown[0].value for message in page.chat_message][1:]


def in_flight() -> None:
    """Stop the page's run while the request is on its way, as the browser sees it."""
    st.stop()


# --- Initial load and conversation start ------------------------------------------------


def test_the_initial_page(open_page: Callable[[Backend], AppTest]) -> None:
    backend = Backend()
    page = open_page(backend)

    assert page.title[0].value == "Customer Support"
    assert [m.name for m in page.chat_message] == ["assistant"]  # the welcome
    assert "Popular questions" in [c.value for c in page.caption]
    assert len([b for b in page.button if b.key and b.key.startswith("example-")]) == 3
    assert not page.chat_input[0].disabled
    assert not page.error
    assert backend.count("/conversations") == 1 and backend.count("/chat") == 0
    state = state_of(page)
    assert state.conversation_status is ConversationStatus.ACTIVE
    assert state.expires_at == EXPIRES
    page.run()
    page.run()
    assert backend.count("/conversations") == 1  # reruns never start another


# --- Sending, loading, replies ----------------------------------------------------------


def test_the_loading_state_while_a_reply_is_awaited(
    open_page: Callable[[Backend], AppTest],
) -> None:
    backend = Backend(in_flight)
    page = say(open_page(backend), "What is your return policy?")

    # Frozen while the request is in flight: what the customer sees meanwhile.
    assert page.chat_input[0].disabled
    assert page.chat_input[0].proto.placeholder == "Waiting for the reply..."
    assert roles(page) == ["user", "assistant"]  # the message, and the reply's placeholder
    assert "Not answered yet" not in [c.value for c in page.caption]  # it is being sent
    state = state_of(page)
    assert state.request_status is RequestStatus.SENDING
    assert state.messages[-1].delivery is Delivery.PENDING
    assert not page.error


def test_an_interrupted_send_resumes_with_the_same_id(
    open_page: Callable[[Backend], AppTest],
) -> None:
    backend = Backend(in_flight, {"answer": "Most items can be returned within 30 days."})
    page = say(open_page(backend), "What is your return policy?")

    page.run()  # the next run (any interaction) finishes the send

    assert roles(page) == ["user", "assistant"]
    assert state_of(page).messages[-1].content == "Most items can be returned within 30 days."
    first, second = backend.chat_ids()
    assert first == second  # the same logical message, the same ID


def test_submitting_while_a_message_is_in_flight_sends_nothing_twice(
    open_page: Callable[[Backend], AppTest],
) -> None:
    backend = Backend(in_flight, {"answer": "First answer."})
    page = say(open_page(backend), "First question")

    # The input is disabled while the reply is awaited: AppTest, like a browser, refuses
    # to type into it.
    with pytest.raises(AppTestError, match="disabled"):
        page.chat_input[0].set_value("Second question").run()
    # Should a submission get through anyway, it is refused and kept, not lost.
    assert state_of(page).queue_message("Second question", max_chars=4000) is None
    assert state_of(page).take_draft() == "Second question"

    page.run()  # the first message finishes, once
    assert roles(page) == ["user", "assistant"]
    assert backend.count("/chat") == 2  # the interrupted try and its resend...
    assert len(set(backend.chat_ids())) == 1  # ...with the same ID
    assert not page.chat_input[0].disabled


def test_an_answer_with_sources_and_a_follow_up(open_page: Callable[[Backend], AppTest]) -> None:
    backend = Backend(
        {"answer": "Most items can be returned within 30 days.", "sources": [SOURCE_RETURNS]},
        {
            "answer": "Returns are free if the item is faulty.",
            "sources": [SOURCE_RETURN_SHIPPING],
            "generation_method": "llm_grounded",
        },
    )
    page = say(open_page(backend), "What is your return policy?")
    say(page, "Do I have to pay to send it back?")

    assert roles(page) == ["user", "assistant", "user", "assistant"]
    assert "Do I have to pay to send it back" in texts(page)[2]
    expanders = [e for e in page.expander if e.label == "Sources used"]
    assert len(expanders) == 2  # one per answer, each with its own source
    assert "What is your return policy" in expanders[0].markdown[-1].value
    assert "return shipping" in expanders[1].markdown[-1].value
    captions = [c.value for c in page.caption]
    assert "Answer from our help articles" in captions
    assert "AI-written summary of our help articles" in captions
    assert backend.count("/chat") == 2


# --- Errors and expiry ------------------------------------------------------------------


def test_an_error_state_keeps_the_message_and_offers_a_retry(
    open_page: Callable[[Backend], AppTest],
) -> None:
    unavailable = httpx.Response(
        503,
        json={"error": {"code": "SERVICE_UNAVAILABLE", "message": "m", "request_id": "req-503"}},
    )
    backend = Backend(unavailable, unavailable, unavailable, {"answer": "Here you go."})
    page = say(open_page(backend), "Where is my order?")

    assert "temporarily unavailable" in page.error[0].value
    assert "Reference: req-503" in [c.value for c in page.caption]
    assert roles(page) == ["user"]
    assert any(c.value.endswith("Not answered yet") for c in page.caption)
    next(b for b in page.button if b.label == "Try again").click().run()
    assert roles(page) == ["user", "assistant"] and not page.error
    assert len(set(backend.chat_ids())) == 1  # every attempt with the same ID


def test_a_conversation_that_expires_while_the_page_is_open(
    open_page: Callable[[Backend], AppTest],
) -> None:
    backend = Backend()
    page = open_page(backend)
    state_of(page).expires_at = datetime.now(UTC) - timedelta(seconds=1)

    page.run()  # any interaction notices it

    assert state_of(page).conversation_status is ConversationStatus.EXPIRED
    assert page.chat_input[0].disabled
    assert page.chat_input[0].proto.placeholder == "This conversation has ended"
    assert "This conversation has ended." in [i.value for i in page.info]
    next(b for b in page.button if b.label == "Start a new conversation").click().run()
    assert state_of(page).conversation_status is ConversationStatus.ACTIVE
    assert backend.count("/conversations") == 2 and backend.count("/chat") == 0


def test_an_expired_reply_from_the_backend_ends_the_conversation(
    open_page: Callable[[Backend], AppTest],
) -> None:
    expired = httpx.Response(
        410, json={"error": {"code": "SESSION_EXPIRED", "message": "m", "request_id": "r"}}
    )
    page = say(open_page(Backend(expired)), "Hello?")
    assert "This conversation has expired" in page.error[0].value
    assert roles(page) == []  # not shown as sent...
    assert page.chat_input[0].proto.value == "Hello?"  # ...but kept for the next one


# --- New conversation and delete --------------------------------------------------------


def test_new_conversation_and_delete_confirmation(
    open_page: Callable[[Backend], AppTest], monkeypatch: pytest.MonkeyPatch
) -> None:
    deleted: list[str] = []
    backend = Backend({"answer": "An answer."})
    original = backend.handle

    def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE" or request.url.path.endswith("/close"):
            backend.requests.append((request.method, request.url.path, None))
        if request.method == "DELETE":
            deleted.append(request.url.path)
            return httpx.Response(204)
        if request.url.path.endswith("/close"):
            return httpx.Response(
                200, json={**CREATED, "status": "closed", "updated_at": CREATED["created_at"]}
            )
        return original(request)

    monkeypatch.setattr(backend, "handle", handle)
    page = say(open_page(backend), "Question")

    next(b for b in page.button if b.label == "New conversation").click().run()
    assert roles(page) == [] and backend.count("/conversations") == 2
    assert backend.count("/close") == 1  # the old one is closed, not deleted

    popover = next(
        p for p in page.sidebar.get("popover") if p.proto.popover.label == "Delete conversation"
    )
    assert "can't be undone" in popover.markdown[0].value
    assert deleted == []  # opening the confirmation deletes nothing
    page.button(key="confirm-delete").click().run()
    assert len(deleted) == 1 and state_of(page).credentials is None
    page.run()
    assert backend.count("/conversations") == 2  # no new conversation until asked
