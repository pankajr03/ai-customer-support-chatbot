"""The Streamlit frontend's session state and actions (Stage 8, Task 5).

``ChatState`` is plain Python stored in ``st.session_state``; ``frontend.actions`` pairs
API calls with state changes. Both are tested here without Streamlit, against a fake
API client that records calls and plays back scripted results.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from frontend import actions
from frontend.api_client import (
    ApiError,
    ChatReply,
    ConversationCreated,
    ConversationHistory,
    ConversationInfo,
    ConversationStatus,
    Credentials,
    Decision,
    ErrorKind,
    HistoryMessage,
    SourceReference,
)
from frontend.formatting import error_text, expiry_text, plain_markdown
from frontend.state import (
    STATE_KEY,
    ChatState,
    Delivery,
    ErrorNotice,
    RequestStatus,
    get_chat_state,
)

TOKEN = "cst_" + "t0k3n" * 8  # noqa: S105 - a fake token
NOW = datetime(2026, 10, 15, 9, 30, tzinfo=UTC)
SOURCE = SourceReference(faq_id="faq_027", question="How do I reset my password?")


def created(session_id: uuid.UUID | None = None) -> ConversationCreated:
    return ConversationCreated(
        session_id=session_id or uuid.uuid4(),
        access_token=TOKEN,  # type: ignore[arg-type]
        token_type="Bearer",  # noqa: S106 - the scheme name
        status=ConversationStatus.ACTIVE,
        created_at=NOW,
        expires_at=NOW + timedelta(hours=1),
    )


def info(session_id: uuid.UUID, status: ConversationStatus) -> ConversationInfo:
    return ConversationInfo(
        session_id=session_id, status=status, created_at=NOW, updated_at=NOW, expires_at=None
    )


class FakeClient:
    """Records calls; ``script`` maps a method name to a list of results or errors."""

    def __init__(self, **script: list[Any]) -> None:
        self.script = script
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.session_id = uuid.uuid4()

    def _next(self, name: str, *args: Any) -> Any:
        self.calls.append((name, args))
        results = self.script.get(name)
        result = results.pop(0) if results else None
        if isinstance(result, Exception):
            raise result
        return result

    def create_conversation(self) -> ConversationCreated:
        return self._next("create") or created(self.session_id)

    def send_message(self, credentials: Credentials, text: str, mid: uuid.UUID) -> ChatReply:
        return self._next("send", credentials, text, mid) or ChatReply(
            session_id=credentials.session_id,
            message_id=uuid.uuid4(),
            client_message_id=mid,
            request_id="r1",
            decision=Decision.ANSWERED,
            answer=f"Answer to {text}",
            generation_method="approved_faq",
            sources=[SOURCE],
            created_at=NOW,
        )

    def close_conversation(self, credentials: Credentials) -> ConversationInfo:
        return self._next("close", credentials) or info(
            credentials.session_id, ConversationStatus.CLOSED
        )

    def delete_conversation(self, credentials: Credentials) -> None:
        self._next("delete", credentials)

    def get_history(self, credentials: Credentials) -> ConversationHistory:
        return self._next("history", credentials)

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]


def send(state: ChatState, client: FakeClient, text: str = "Hello") -> None:
    state.queue_message(text)
    actions.send_pending(state, client)  # type: ignore[arg-type]


# --- State ----------------------------------------------------------------------------


def test_state_is_created_once_per_session_store() -> None:
    store: dict[str, Any] = {}
    state = get_chat_state(store)
    assert store[STATE_KEY] is state
    assert get_chat_state(store) is state  # a rerun gets the same object
    assert get_chat_state({}) is not state  # another browser session gets its own


def test_a_fresh_state_has_no_conversation() -> None:
    state = ChatState()
    assert state.credentials is None and state.conversation_status is None
    assert state.can_send and not state.is_sending and not state.has_ended


def test_queueing_a_message_gives_it_a_new_id_and_disables_input() -> None:
    state = ChatState()
    first = state.queue_message("  Hello  ")
    assert first is not None and first.text == "Hello"
    assert state.is_sending and not state.can_send
    assert state.messages[-1].delivery is Delivery.PENDING
    assert state.messages[-1].message_id == str(first.client_message_id)
    assert state.queue_message("Another") is None  # not while sending


def test_blank_messages_are_not_queued() -> None:
    state = ChatState()
    assert state.queue_message(" \n\t ") is None
    assert state.messages == [] and not state.is_sending


def test_the_token_is_masked_in_the_state() -> None:
    state = ChatState()
    state.start(created())
    assert TOKEN not in repr(state) and TOKEN not in str(state)


# --- Sending --------------------------------------------------------------------------


def test_the_first_message_starts_the_conversation() -> None:
    client, state = FakeClient(), ChatState()
    send(state, client)
    assert client.names() == ["create", "send"]
    assert state.credentials is not None and state.credentials.session_id == client.session_id
    assert state.conversation_status is ConversationStatus.ACTIVE
    assert [m.role for m in state.messages] == ["user", "assistant"]
    assert state.messages[0].delivery is Delivery.SENT
    assert state.messages[1].sources == (SOURCE,)
    assert state.request_status is RequestStatus.IDLE and state.pending is None


def test_later_messages_reuse_the_conversation() -> None:
    client, state = FakeClient(), ChatState()
    send(state, client, "One")
    send(state, client, "Two")
    assert client.names() == ["create", "send", "send"]
    ids = {args[2] for name, args in client.calls if name == "send"}
    assert len(ids) == 2  # one client_message_id per message


def test_nothing_is_sent_while_idle() -> None:
    client, state = FakeClient(), ChatState()
    actions.send_pending(state, client)  # type: ignore[arg-type]
    assert client.calls == []


def test_a_retryable_failure_keeps_the_message_and_its_id_for_a_retry() -> None:
    client = FakeClient(send=[ApiError(ErrorKind.TIMEOUT)])
    state = ChatState()
    send(state, client)
    assert state.request_status is RequestStatus.FAILED and state.can_retry
    assert state.last_error == ErrorNotice(kind=ErrorKind.TIMEOUT)
    assert state.messages[-1].delivery is Delivery.PENDING

    assert state.retry_pending()
    actions.send_pending(state, client)  # type: ignore[arg-type]
    first_id, retry_id = (args[2] for name, args in client.calls if name == "send")
    assert first_id == retry_id
    assert state.request_status is RequestStatus.IDLE and state.last_error is None
    assert [m.role for m in state.messages] == ["user", "assistant"]


def test_typing_a_new_message_gives_up_the_failed_one() -> None:
    client = FakeClient(send=[ApiError(ErrorKind.SERVICE_UNAVAILABLE)])
    state = ChatState()
    send(state, client, "Lost")
    send(state, client, "Next")
    assert [m.delivery for m in state.messages if m.role == "user"] == [
        Delivery.NOT_SENT,
        Delivery.SENT,
    ]


def test_an_invalid_message_is_not_kept_for_a_retry() -> None:
    error = ApiError(ErrorKind.INVALID_MESSAGE, validation_types=("string_too_long",))
    state = ChatState()
    send(state, FakeClient(send=[error]))
    assert not state.can_retry and state.pending is None
    assert state.messages[-1].delivery is Delivery.NOT_SENT
    assert state.can_send  # the customer can type a shorter one


@pytest.mark.parametrize("kind", [ErrorKind.SESSION_EXPIRED, ErrorKind.SESSION_NOT_FOUND])
def test_an_expired_or_lost_conversation_drops_its_credentials(kind: ErrorKind) -> None:
    state = ChatState()
    client = FakeClient(send=[None, ApiError(kind)])
    send(state, client, "One")
    send(state, client, "Two")
    assert state.credentials is None
    assert state.conversation_status is ConversationStatus.EXPIRED
    assert state.has_ended and not state.can_send and not state.can_retry
    assert state.queue_message("Three") is None
    assert len(state.messages) == 3  # the history stays on the page


def test_a_closed_conversation_keeps_its_credentials_for_reading() -> None:
    state = ChatState()
    send(state, FakeClient(send=[None, ApiError(ErrorKind.SESSION_CLOSED)]))
    state.queue_message("Again")
    actions.send_pending(state, FakeClient(send=[ApiError(ErrorKind.SESSION_CLOSED)]))  # type: ignore[arg-type]
    assert state.conversation_status is ConversationStatus.CLOSED
    assert state.credentials is not None and state.has_ended


def test_a_failed_start_keeps_the_message_for_a_retry() -> None:
    state = ChatState()
    send(state, FakeClient(create=[ApiError(ErrorKind.BACKEND_UNREACHABLE)]))
    assert state.credentials is None and state.conversation_status is None
    assert state.can_retry


# --- Sidebar actions ------------------------------------------------------------------


def test_closing_keeps_the_messages_and_stops_input() -> None:
    client, state = FakeClient(), ChatState()
    send(state, client)
    actions.close_conversation(state, client)  # type: ignore[arg-type]
    assert state.conversation_status is ConversationStatus.CLOSED
    assert len(state.messages) == 2 and not state.can_send


def test_deleting_forgets_everything() -> None:
    client, state = FakeClient(), ChatState()
    send(state, client)
    assert actions.delete_conversation(state, client)  # type: ignore[arg-type]
    assert client.names()[-1] == "delete"
    assert state == ChatState()


def test_deleting_a_conversation_the_backend_no_longer_has_still_clears_it() -> None:
    client, state = FakeClient(delete=[ApiError(ErrorKind.SESSION_NOT_FOUND)]), ChatState()
    send(state, client)
    assert actions.delete_conversation(state, client)  # type: ignore[arg-type]
    assert state == ChatState()


def test_a_failed_delete_keeps_the_conversation_to_try_again() -> None:
    client = FakeClient(delete=[ApiError(ErrorKind.BACKEND_UNREACHABLE)])
    state = ChatState()
    send(state, client)
    assert not actions.delete_conversation(state, client)  # type: ignore[arg-type]
    assert state.credentials is not None
    assert state.last_error == ErrorNotice(kind=ErrorKind.BACKEND_UNREACHABLE)


def test_a_new_conversation_closes_the_old_one_and_clears_the_page() -> None:
    client, state = FakeClient(), ChatState()
    send(state, client)
    actions.start_new_conversation(state, client)  # type: ignore[arg-type]
    assert client.names()[-1] == "close"
    assert state == ChatState()
    send(state, client)
    assert client.names()[-2:] == ["create", "send"]


def test_a_new_conversation_starts_even_if_closing_the_old_one_fails() -> None:
    client = FakeClient(close=[ApiError(ErrorKind.SERVICE_UNAVAILABLE)])
    state = ChatState()
    send(state, client)
    actions.start_new_conversation(state, client)  # type: ignore[arg-type]
    assert state == ChatState()


def test_reloading_shows_the_backends_history() -> None:
    client, state = FakeClient(), ChatState()
    send(state, client)
    stored = [
        HistoryMessage(
            message_id=uuid.uuid4(),
            role=role,
            content=content,
            created_at=NOW,
            decision=decision,
            source_faq_ids=ids,
        )
        for role, content, decision, ids in [
            ("user", "Hello", None, []),
            ("assistant", "Answer", Decision.ANSWERED, ["faq_027"]),
            ("user", "More", None, []),
            ("assistant", "Sorry", Decision.NO_MATCH, []),
        ]
    ]
    client.script["history"] = [
        ConversationHistory(status=ConversationStatus.ACTIVE, messages=stored, complete=True)
    ]
    actions.reload_history(state, client)  # type: ignore[arg-type]
    assert [m.content for m in state.messages] == ["Hello", "Answer", "More", "Sorry"]
    assert state.messages[1].sources == (SOURCE,)  # known from this page's own reply


# --- Formatting -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "![tracker](https://evil.example/pixel.png)",
        "[click me](javascript:alert(1))",
        "<img src=x onerror=alert(1)>",
        "# Heading",
        ":red[colour] :material/home: $x^2$",
        "**bold** _it_ `code` ~~strike~~ | table |",
    ],
)
def test_message_text_cannot_become_markup(text: str) -> None:
    escaped = plain_markdown(text)
    for character in "![]()<>#:$*_`~|":
        assert character not in escaped.replace("\\" + character, "")


def test_line_breaks_are_kept() -> None:
    assert plain_markdown("one\ntwo\r\n\nthree") == "one  \ntwo  \n\nthree  "


def test_error_text_is_specific_where_it_helps() -> None:
    too_long = ErrorNotice(
        ErrorKind.INVALID_MESSAGE, validation_types=("string_too_long",), max_length=4000
    )
    assert error_text(too_long) == (
        "Your message is too long (at most 4,000 characters). Please shorten it."
    )
    limited = ErrorNotice(ErrorKind.RATE_LIMITED, retry_after=30)
    assert error_text(limited).endswith("try again in about 30 s.")
    for kind in ErrorKind:
        text = error_text(ErrorNotice(kind))
        assert text and kind.value not in text  # wording, never internal names


def test_expiry_text() -> None:
    assert expiry_text(None) is None
    assert expiry_text(NOW + timedelta(minutes=42, seconds=5), now=NOW) == "Expires in 42 min"
    assert expiry_text(NOW + timedelta(seconds=20), now=NOW) == "Expires in less than a minute"
    assert expiry_text(NOW + timedelta(hours=5), now=NOW) == "Expires in 5 h"
