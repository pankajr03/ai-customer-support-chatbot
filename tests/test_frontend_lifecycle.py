"""The whole customer journey through the Streamlit page (Stage 9, Task 5).

Each test drives the real page (``AppTest``) against the real backend in-process
(``make_backend``: real routes, storage, retrieval and query rewriting; only the
embedding model is scripted). Every HTTP request the page sends is recorded, so the
tests can tell "shown once" from "created once".

Each ``AppTest`` is one browser session with its own ``st.session_state``. Opening a
second one on the same backend is what happens after a browser refresh that loses the
session: see "What survives a lost browser session" in frontend/README.md.
"""

from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from streamlit.testing.v1 import AppTest

from frontend import resources
from frontend.api_client import (
    ApiError,
    BackendStatus,
    ChatApiClient,
    ConversationStatus,
    Credentials,
    Decision,
    ErrorKind,
)
from frontend.config import FrontendSettings
from frontend.state import STATE_KEY, ChatState
from tests.test_api_workflows import FOLLOW_UP, RESET, SHIPPING

APP = Path(__file__).resolve().parents[1] / "frontend" / "app.py"
CREATE = ("POST", "/api/v1/conversations")
WRONG_TOKEN = SecretStr("cst_" + "x" * 43)  # a fake token


class Server:
    """One backend shared by any number of browser sessions, with a request log."""

    def __init__(self, backend: TestClient) -> None:
        self.backend = backend
        self.requests: list[tuple[str, str]] = []
        backend.event_hooks = {"request": [self._record], "response": []}
        self.api = ChatApiClient(http_client=backend, sleep=lambda seconds: None)

    def _record(self, request: httpx.Request) -> None:
        self.requests.append((request.method, request.url.path))

    def count(self, method: str, suffix: str) -> int:
        return sum(1 for m, path in self.requests if m == method and path.endswith(suffix))

    def history(self, credentials: Credentials) -> list[tuple[str, str]]:
        """What the backend stored, as (role, content), read without recording."""
        hooks, self.backend.event_hooks = self.backend.event_hooks, {"request": [], "response": []}
        try:
            stored = self.api.get_history(credentials)
        finally:
            self.backend.event_hooks = hooks
        return [(m.role, m.content) for m in stored.messages]


@pytest.fixture
def server(make_backend: Callable[..., TestClient], monkeypatch: pytest.MonkeyPatch) -> Server:
    server = Server(make_backend())
    status = lambda _client: BackendStatus.ONLINE  # noqa: E731
    status.clear = lambda: None  # type: ignore[attr-defined]
    monkeypatch.setattr(resources, "get_api_client", lambda: server.api)
    monkeypatch.setattr(resources, "backend_status", status)
    monkeypatch.setattr(resources, "get_settings", lambda: FrontendSettings(_env_file=None))
    return server


@pytest.fixture
def open_browser(server: Server) -> Iterator[Callable[[], AppTest]]:
    """Open the page in a new browser session (a new tab, or after a lost session)."""

    def _open() -> AppTest:
        page = AppTest.from_file(str(APP), default_timeout=30)
        page.run()
        assert not page.exception
        return page

    yield _open


def state_of(page: AppTest) -> ChatState:
    return page.session_state[STATE_KEY]


def say(page: AppTest, text: str) -> AppTest:
    page.chat_input[0].set_value(text).run()
    assert not page.exception
    return page


def click(page: AppTest, label: str) -> AppTest:
    next(button for button in page.button if button.label == label).click().run()
    assert not page.exception
    return page


def conversation(page: AppTest) -> list[tuple[str, str]]:
    return [(m.role, m.content) for m in state_of(page).messages]


def roles_shown(page: AppTest) -> list[str]:
    """The chat bubbles on the page, after the welcome message."""
    names = [message.name for message in page.chat_message]
    assert names[0] == "assistant"  # the welcome message
    return names[1:]


def sources_of(page: AppTest) -> list[list[tuple[str, str]]]:
    return [
        [(s.faq_id, s.question) for s in m.sources]
        for m in state_of(page).messages
        if m.role == "assistant"
    ]


# --- The journey ----------------------------------------------------------------------


def test_the_whole_customer_journey(server: Server, open_browser: Callable[[], AppTest]) -> None:
    # 1. Open the page: exactly one backend conversation, credentials in session state.
    page = open_browser()
    state = state_of(page)
    credentials = state.credentials
    assert credentials is not None and state.conversation_status is ConversationStatus.ACTIVE
    assert server.count(*CREATE) == 1
    token = credentials.access_token.get_secret_value()

    # 2. First question: a relevant approved FAQ, stored, shown with its source.
    say(page, RESET)
    first = state_of(page).messages[-1]
    assert first.decision is Decision.ANSWERED
    assert [s.faq_id for s in first.sources] == ["faq_107"]
    assert roles_shown(page) == ["user", "assistant"]
    assert server.history(credentials) == conversation(page)  # persisted, same order

    # 3. A follow-up with no topic of its own, resolved from the history to the same FAQ.
    say(page, FOLLOW_UP)
    follow_up = state_of(page).messages[-1]
    assert follow_up.decision is Decision.ANSWERED
    assert [s.faq_id for s in follow_up.sources] == ["faq_107"]
    assert roles_shown(page) == ["user", "assistant", "user", "assistant"]
    expected = conversation(page)
    assert [content for role, content in expected if role == "user"] == [RESET, FOLLOW_UP]
    assert server.history(credentials) == expected

    # 4. Reruns (any interaction) and a refresh from the backend change nothing.
    sources = sources_of(page)
    for _ in range(3):
        page.run()
    click(page, "Refresh messages")
    page.run()
    assert conversation(page) == expected  # no loss, no duplicates, same order
    assert roles_shown(page) == ["user", "assistant", "user", "assistant"]
    assert sources_of(page) == sources  # source references kept
    assert state_of(page).credentials == credentials  # still the same conversation
    assert server.count(*CREATE) == 1  # reruns never create another
    assert server.count("POST", "/chat") == 2  # each message answered once
    assert token not in str(page._tree)  # the token is never on the page

    # 5. Delete it: gone on the backend, and the page forgets the credentials.
    page.button(key="confirm-delete").click().run()
    assert state_of(page).credentials is None and state_of(page).messages == []
    with pytest.raises(ApiError) as caught:
        server.api.get_history(credentials)
    assert caught.value.kind is ErrorKind.SESSION_NOT_FOUND
    page.run()
    assert server.count(*CREATE) == 1  # no new conversation until the customer asks


def test_closing_keeps_the_conversation_readable_but_ends_it(
    server: Server, open_browser: Callable[[], AppTest]
) -> None:
    page = say(open_browser(), RESET)
    credentials = state_of(page).credentials
    assert credentials is not None
    expected = conversation(page)

    click(page, "End conversation")
    page.run()

    assert state_of(page).conversation_status is ConversationStatus.CLOSED
    assert page.chat_input[0].disabled
    assert conversation(page) == expected  # still shown
    assert server.api.get_conversation(credentials).status is ConversationStatus.CLOSED
    assert server.history(credentials) == expected  # still stored and readable
    assert server.count(*CREATE) == 1


# --- Sessions -------------------------------------------------------------------------


def test_a_lost_browser_session_starts_over_and_the_old_conversation_stays_private(
    server: Server, open_browser: Callable[[], AppTest]
) -> None:
    before = say(open_browser(), RESET)
    old = state_of(before).credentials
    assert old is not None
    old_messages = conversation(before)

    # A refresh that loses st.session_state is a new browser session.
    after = open_browser()

    state = state_of(after)
    assert state.credentials is not None and state.credentials != old
    assert state.messages == [] and roles_shown(after) == []  # nothing of the old one
    assert server.count(*CREATE) == 2  # one per browser session, never more
    # The old conversation is intact on the backend, reachable only with its token,
    # which no page holds any more. It expires there (CONVERSATION_TTL_MINUTES).
    assert server.history(old) == old_messages
    with pytest.raises(ApiError) as caught:
        server.api.get_history(Credentials(session_id=old.session_id, access_token=WRONG_TOKEN))
    assert caught.value.kind is ErrorKind.SESSION_NOT_FOUND


def test_two_browser_sessions_never_see_each_others_messages(
    server: Server, open_browser: Callable[[], AppTest]
) -> None:
    alice, bob = open_browser(), open_browser()
    say(alice, RESET)
    say(bob, SHIPPING)
    say(alice, FOLLOW_UP)
    click(bob, "Refresh messages")
    click(alice, "Refresh messages")

    assert state_of(alice).credentials != state_of(bob).credentials
    assert [c for r, c in conversation(alice) if r == "user"] == [RESET, FOLLOW_UP]
    assert [c for r, c in conversation(bob) if r == "user"] == [SHIPPING]
    assert roles_shown(bob) == ["user", "assistant"]
    # Bob's history never helped answer Alice's follow-up, and the other way round.
    assert [s.faq_id for s in state_of(alice).messages[-1].sources] == ["faq_107"]
    assert {s.faq_id for s in state_of(bob).messages[-1].sources} == {"faq_103", "faq_104"}


def test_a_reply_the_page_lost_comes_back_with_its_sources(
    server: Server, open_browser: Callable[[], AppTest]
) -> None:
    # As after a timeout whose reply was stored anyway: the backend has the reply, the
    # page does not. Refreshing brings it back with the approved source questions, not
    # just the FAQ IDs.
    page = say(open_browser(), RESET)
    expected_sources = sources_of(page)
    state_of(page).messages = []

    click(page, "Refresh messages")

    assert roles_shown(page) == ["user", "assistant"]
    assert sources_of(page) == expected_sources
    assert expected_sources[0][0][1]  # the question, not only the ID


def test_message_ids_follow_the_backends_order(
    server: Server, open_browser: Callable[[], AppTest]
) -> None:
    page = say(say(open_browser(), RESET), FOLLOW_UP)
    credentials = state_of(page).credentials
    assert credentials is not None
    stored = server.api.get_history(credentials).messages
    replies = [m.message_id for m in state_of(page).messages if m.role == "assistant"]
    assert replies == [str(m.message_id) for m in stored if m.role == "assistant"]
    assert [m.created_at for m in stored] == sorted(m.created_at for m in stored)


def test_a_message_lost_on_the_way_can_be_sent_again_from_the_page(
    server: Server, open_browser: Callable[[], AppTest]
) -> None:
    from tests.test_frontend_sync import Flaky

    flaky = Flaky(server.backend._transport)
    server.backend._transport = flaky
    flaky.plan = ["lost"]
    page = say(open_browser(), RESET)  # timed out: the outcome is unknown
    assert "Try again" in [b.label for b in page.button]
    say(page, SHIPPING)  # the customer moves on instead

    assert "May not have been sent" in "\n".join(c.value for c in page.caption)
    lost = next(m for m in state_of(page).messages if m.content == RESET)
    click(page, "Send again")

    credentials = state_of(page).credentials
    assert credentials is not None
    assert conversation(page) == server.history(credentials)  # same as stored, same order
    assert lost.message_id in [m.message_id for m in state_of(page).messages]  # same ID
    assert "Send again" not in [b.label for b in page.button]
    assert server.count("POST", "/chat") == 3  # lost, SHIPPING, RESET once
