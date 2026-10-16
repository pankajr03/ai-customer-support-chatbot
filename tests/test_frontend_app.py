"""The Streamlit page itself (Stage 8, Tasks 3 and 6-10), run headless with AppTest.

The page's API client is pointed at the real backend in-process (``make_backend``), so
these tests cover the whole path: chat input -> session state -> HTTP -> FastAPI ->
reply rendered in a chat bubble. Replies the scripted backend cannot produce on demand
(retrieval_unavailable) come from a scripted transport.
"""

import json
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from streamlit.testing.v1 import AppTest

from frontend import resources
from frontend.api_client import ChatApiClient, ConversationStatus
from frontend.config import FrontendSettings
from frontend.state import STATE_KEY, ChatState
from tests.test_api_workflows import RESET, SHIPPING, UNKNOWN

APP = Path(__file__).resolve().parents[1] / "frontend" / "app.py"
CONVERSATIONS = "/api/v1/conversations"


@pytest.fixture
def run_page(
    monkeypatch: pytest.MonkeyPatch, make_backend: Callable[..., TestClient]
) -> Callable[..., AppTest]:
    """Open the page with a given API client (default: the in-process backend)."""

    def _run(
        client: ChatApiClient | None = None, *, online: bool = True, **backend: Any
    ) -> AppTest:
        api = client or ChatApiClient(
            http_client=make_backend(**backend), sleep=lambda seconds: None
        )
        monkeypatch.setattr(resources, "get_api_client", lambda: api)
        monkeypatch.setattr(resources, "backend_is_healthy", _health(online))
        monkeypatch.setattr(resources, "get_settings", lambda: FrontendSettings(_env_file=None))
        page = AppTest.from_file(str(APP), default_timeout=30)
        page.run()
        assert not page.exception
        return page

    return _run


def _health(online: bool) -> Any:
    def healthy(_client: ChatApiClient) -> bool:
        return online

    healthy.clear = lambda: None  # type: ignore[attr-defined]
    return healthy


def state_of(page: AppTest) -> ChatState:
    return page.session_state[STATE_KEY]


def walk(node: Any) -> Iterator[Any]:
    yield node
    for child in getattr(node, "children", {}).values():
        yield from walk(child)


def rendered(page: AppTest) -> str:
    """Everything the page sent to the browser, as protobuf text (quotes escaped)."""
    return "\n".join(str(getattr(node, "proto", "")) for node in walk(page._tree))


def captions(page: AppTest) -> str:
    return "\n".join(caption.value for caption in page.caption)


def say(page: AppTest, text: str) -> AppTest:
    page.chat_input[0].set_value(text).run()
    assert not page.exception
    return page


def bubbles(page: AppTest) -> list[str]:
    return [message.name for message in page.chat_message]


def click(page: AppTest, label: str) -> AppTest:
    next(button for button in page.button if button.label == label).click().run()
    assert not page.exception
    return page


def scripted_backend(*replies: dict[str, Any]) -> tuple[ChatApiClient, list[httpx.Request]]:
    """A backend that creates conversations and answers chats with ``replies``."""
    session_id = str(uuid.uuid4())
    queue = list(replies)
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == CONVERSATIONS:
            return httpx.Response(201, json={
                "session_id": session_id, "access_token": "cst_x", "token_type": "Bearer",
                "status": "active", "created_at": "2026-10-15T09:30:00Z", "expires_at": None,
            })  # fmt: skip
        body = json.loads(request.content)
        return httpx.Response(200, json={
            "session_id": session_id, "message_id": str(uuid.uuid4()),
            "client_message_id": body["client_message_id"], "request_id": "r",
            "generation_method": "fallback", "sources": [],
            "created_at": "2026-10-15T09:31:00Z", **queue.pop(0),
        })  # fmt: skip

    http = httpx.Client(base_url="http://backend.test", transport=httpx.MockTransport(handle))
    return ChatApiClient(http_client=http, sleep=lambda s: None), requests


# --- Task 6: opening the page ---------------------------------------------------------


def test_opening_the_page_starts_one_conversation(run_page: Callable) -> None:
    page = run_page()
    assert page.title[0].value == "Customer Support"
    assert bubbles(page) == ["assistant"]  # the welcome message
    assert "Popular questions" in captions(page)
    state = state_of(page)
    assert state.conversation_status is ConversationStatus.ACTIVE
    credentials = state.credentials
    page.run()  # a rerun
    page.run()
    assert state_of(page).credentials == credentials  # no second conversation
    assert not page.chat_input[0].disabled and not page.error


def test_an_offline_backend_is_reported_with_a_retry(run_page: Callable) -> None:
    page = run_page(online=False)
    assert state_of(page).credentials is None
    assert "can't reach the support service" in page.error[0].value
    assert "Unavailable" in rendered(page)  # the sidebar badge
    click(page, "Try again")  # the backend (in-process) is actually reachable
    assert state_of(page).conversation_status is ConversationStatus.ACTIVE
    assert not page.error


def test_a_failed_start_shows_no_technical_details(run_page: Callable) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("[Errno 10061] connection refused by 127.0.0.1:8000")

    http = httpx.Client(base_url="http://backend.test", transport=httpx.MockTransport(refuse))
    page = run_page(ChatApiClient(http_client=http))
    assert "can't reach the support service" in page.error[0].value
    assert "10061" not in rendered(page) and "Traceback" not in rendered(page)
    assert any(button.label == "Try again" for button in page.button)


# --- Task 7: sending messages ---------------------------------------------------------


def test_a_question_is_answered_with_its_sources(run_page: Callable) -> None:
    page = say(run_page(), RESET)
    assert bubbles(page) == ["assistant", "user", "assistant"]
    state = state_of(page)
    assert [source.faq_id for source in state.messages[-1].sources] == ["faq_107"]
    assert [expander.label for expander in page.main.expander] == ["Sources used"]
    assert "The help articles this reply was based on." in captions(page)
    assert not page.chat_input[0].disabled and not page.error


def test_the_token_never_reaches_the_page(run_page: Callable) -> None:
    page = say(run_page(), RESET)
    credentials = state_of(page).credentials
    assert credentials is not None
    token = credentials.access_token.get_secret_value()
    assert token not in rendered(page)
    assert token not in repr(state_of(page))


def test_customer_text_is_shown_literally(run_page: Callable) -> None:
    page = say(run_page(), "![x](https://evil.example/p.png) <b>hi</b> return")
    shown = page.chat_message[1].markdown[0].value
    assert "\\!\\[x\\]\\(https\\:" in shown and "\\<b\\>" in shown


def test_a_refused_message_is_put_back_into_the_input(run_page: Callable) -> None:
    # The backend allows 100 characters; the page was configured for 4,000.
    page = say(run_page(max_customer_message_characters=100), "a" * 150)
    assert bubbles(page) == ["assistant"]  # not shown as sent
    assert "too long (at most 100 characters)" in page.error[0].value
    assert "a" * 150 in rendered(page)  # back in the chat input
    assert "max_chars: 100" in rendered(page)  # the input now knows the limit


def test_the_input_is_disabled_while_a_message_is_answered(run_page: Callable) -> None:
    page = run_page()
    # The browser disables the input as soon as it is submitted, until the run ends.
    assert "submit_mode: SUBMIT_MODE_DISABLE" in rendered(page)


# --- Task 8: decisions ----------------------------------------------------------------


def test_ambiguous_replies_offer_the_questions_as_buttons(run_page: Callable) -> None:
    page = say(run_page(), SHIPPING)
    choices = [button for button in page.button if "choice" in (button.key or "")]
    assert len(choices) == 2
    assert not page.chat_input[0].disabled  # the customer can also type a reply
    choices[0].click().run()
    assert not page.exception
    user_messages = [m for m in state_of(page).messages if m.role == "user"]
    assert len(user_messages) == 2  # the chosen question was sent as the next message


def test_a_no_match_reply_is_shown_as_it_is(run_page: Callable) -> None:
    page = say(run_page(), UNKNOWN)
    assert not page.main.expander  # no sources section
    reply = state_of(page).messages[-1]
    assert page.chat_message[2].markdown[0].value.startswith("Sorry, I couldn")
    assert reply.content.startswith("Sorry, I couldn't find a reliable answer")
    assert "couldn't find this in our help articles" in captions(page)
    assert "%" not in captions(page)  # no confidence figures


def test_retrieval_unavailable_offers_to_ask_again(run_page: Callable) -> None:
    unavailable = {
        "decision": "retrieval_unavailable",
        "answer": "Sorry, I can't search our help articles right now.",
    }
    answered = {
        "decision": "answered",
        "answer": "Use Forgot Password.",
        "generation_method": "approved_faq",
        "sources": [{"faq_id": "faq_027", "question": "How do I reset my password?"}],
    }
    client, requests = scripted_backend(unavailable, answered)
    page = say(run_page(client), "Password help")
    assert "briefly unavailable" in captions(page)
    click(page, "Ask again")
    chats = [json.loads(r.content) for r in requests if r.url.path.endswith("/chat")]
    assert [c["message"] for c in chats] == ["Password help", "Password help"]
    assert chats[0]["client_message_id"] != chats[1]["client_message_id"]  # a new message
    assert state_of(page).messages[-1].content == "Use Forgot Password."


# --- Task 10: sidebar -----------------------------------------------------------------


def test_a_new_conversation_creates_a_new_backend_session(run_page: Callable) -> None:
    page = say(run_page(), RESET)
    old = state_of(page).credentials
    click(page, "New conversation")
    state = state_of(page)
    assert state.credentials is not None and state.credentials != old
    assert state.messages == [] and bubbles(page) == ["assistant"]


def test_ending_the_conversation_disables_input(run_page: Callable) -> None:
    page = click(say(run_page(), RESET), "End conversation")
    assert state_of(page).conversation_status is ConversationStatus.CLOSED
    assert page.chat_input[0].disabled
    assert len(state_of(page).messages) == 2  # still shown
    assert any(button.label == "Start a new conversation" for button in page.button)


def test_deleting_the_conversation_clears_the_page(run_page: Callable) -> None:
    page = say(run_page(), RESET)
    page.button(key="confirm-delete").click().run()
    assert not page.exception
    state = state_of(page)
    assert state.credentials is None and state.messages == []
    assert bubbles(page) == ["assistant"]
    page.run()
    assert state_of(page).credentials is None  # no conversation started on its own
    say(page, RESET)  # until the customer writes again
    assert state_of(page).conversation_status is ConversationStatus.ACTIVE


def test_an_unreachable_backend_keeps_the_message_for_a_retry(run_page: Callable) -> None:
    client, _ = scripted_backend()
    page = run_page(client)

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    client._http._transport = httpx.MockTransport(refuse)
    say(page, "Hi")
    assert "can't reach the support service" in page.error[0].value
    assert "Not answered yet" in captions(page)
    assert any(button.label == "Try again" for button in page.button)
    assert "connection refused" not in rendered(page)


# --- Task 11: history -----------------------------------------------------------------


def test_refreshing_shows_the_stored_conversation_once(run_page: Callable) -> None:
    page = say(say(run_page(), RESET), UNKNOWN)
    before = [(m.role, m.content) for m in state_of(page).messages]
    click(page, "Refresh messages")
    state = state_of(page)
    assert [(m.role, m.content) for m in state.messages] == before  # same order, no copies
    assert state.messages[1].generation_method == "approved_faq"  # kept from the page
    assert bubbles(page) == ["assistant", "user", "assistant", "user", "assistant"]
    page.run()  # a rerun shows the same conversation
    assert [(m.role, m.content) for m in state_of(page).messages] == before
