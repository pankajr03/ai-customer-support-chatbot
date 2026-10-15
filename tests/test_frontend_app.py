"""The Streamlit page itself (Stage 8, Task 3), run headless with Streamlit's AppTest.

The page's API client is pointed at the real backend in-process (``make_backend``), so
these tests cover the whole path: chat input -> session state -> HTTP -> FastAPI ->
reply rendered in a chat bubble.
"""

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


@pytest.fixture
def run_page(
    monkeypatch: pytest.MonkeyPatch, make_backend: Callable[..., TestClient]
) -> Callable[..., AppTest]:
    """Start the page with a given API client (default: the in-process backend)."""

    def _run(client: ChatApiClient | None = None, *, online: bool = True) -> AppTest:
        api = client or ChatApiClient(http_client=make_backend(), sleep=lambda seconds: None)
        monkeypatch.setattr(resources, "get_api_client", lambda: api)
        monkeypatch.setattr(resources, "backend_is_healthy", lambda _client: online)
        monkeypatch.setattr(resources, "get_settings", lambda: FrontendSettings(_env_file=None))
        page = AppTest.from_file(str(APP), default_timeout=30)
        page.run()
        assert not page.exception
        return page

    return _run


def state_of(page: AppTest) -> ChatState:
    return page.session_state[STATE_KEY]


def walk(node: Any) -> Iterator[Any]:
    yield node
    for child in getattr(node, "children", {}).values():
        yield from walk(child)


def rendered(page: AppTest) -> str:
    """Everything the page sent to the browser, as text."""
    return "\n".join(str(getattr(node, "proto", "")) for node in walk(page._tree))


def captions(page: AppTest) -> str:
    return "\n".join(caption.value for caption in page.caption)


def say(page: AppTest, text: str) -> AppTest:
    page.chat_input[0].set_value(text).run()
    assert not page.exception
    return page


def bubbles(page: AppTest) -> list[tuple[str, str]]:
    return [
        (message.name, message.markdown[0].value if message.markdown else "")
        for message in page.chat_message
    ]


def test_the_page_opens_with_a_welcome_and_no_conversation(run_page: Callable) -> None:
    page = run_page()
    assert page.title[0].value == "Customer Support"
    assert [name for name, _ in bubbles(page)] == ["assistant"]  # the welcome message
    assert "Popular questions" in [caption.value for caption in page.caption]
    assert not page.chat_input[0].disabled
    assert state_of(page).credentials is None  # nothing is created until a message


def test_a_question_is_answered_with_its_sources(run_page: Callable) -> None:
    page = say(run_page(), RESET)
    assert [name for name, _ in bubbles(page)] == ["assistant", "user", "assistant"]
    state = state_of(page)
    assert state.conversation_status is ConversationStatus.ACTIVE
    assert [source.faq_id for source in state.messages[-1].sources] == ["faq_107"]
    assert any(expander.label == "Source" for expander in page.expander)
    assert "Help article faq_107" in rendered(page)
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
    _, shown = bubbles(page)[1]
    assert "\\!\\[x\\]\\(https\\:" in shown and "\\<b\\>" in shown


def test_ambiguous_replies_offer_the_questions_as_buttons(run_page: Callable) -> None:
    page = say(run_page(), SHIPPING)
    choices = [button for button in page.button if "choice" in (button.key or "")]
    assert len(choices) == 2
    choices[0].click().run()
    assert not page.exception
    user_messages = [m for m in state_of(page).messages if m.role == "user"]
    assert len(user_messages) == 2  # the chosen question was sent as the next message


def test_a_no_match_reply_is_not_presented_as_an_answer(run_page: Callable) -> None:
    page = say(run_page(), UNKNOWN)
    assert not page.expander  # no sources
    assert "couldn't find this in our help articles" in captions(page)


def test_example_questions_send_a_message(run_page: Callable) -> None:
    page = run_page()
    page.button(key="example-0").click().run()
    assert [name for name, _ in bubbles(page)] == ["assistant", "user", "assistant"]


def test_ending_the_conversation_disables_input(run_page: Callable) -> None:
    page = say(run_page(), RESET)
    next(b for b in page.sidebar.button if b.label == "End conversation").click().run()
    assert state_of(page).conversation_status is ConversationStatus.CLOSED
    assert page.chat_input[0].disabled
    assert len(state_of(page).messages) == 2  # still shown


def test_deleting_the_conversation_clears_the_page(run_page: Callable) -> None:
    page = say(run_page(), RESET)
    page.button(key="confirm-delete").click().run()
    assert not page.exception
    assert state_of(page) == ChatState()
    assert [name for name, _ in bubbles(page)] == ["assistant"]


def test_an_unreachable_backend_shows_an_error_and_a_retry(run_page: Callable) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    http = httpx.Client(base_url="http://backend.test", transport=httpx.MockTransport(refuse))
    page = run_page(ChatApiClient(http_client=http, sleep=lambda s: None), online=False)
    assert "offline" in page.warning[0].value  # the banner, before anything is typed
    say(page, "Hi")
    assert "can't reach the support service" in page.error[0].value
    assert any(button.label == "Try again" for button in page.button)
    assert "connection refused" not in rendered(page)  # no internal details
    assert "Not answered yet" in captions(page)
