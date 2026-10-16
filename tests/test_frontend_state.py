"""The Streamlit frontend's session state and actions (Stage 8, Tasks 5-10).

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
from frontend.formatting import (
    error_text,
    expiry_text,
    plain_markdown,
    source_labels,
    unique_sources,
)
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


def reply_for(credentials: Credentials, mid: uuid.UUID, **fields: Any) -> ChatReply:
    return ChatReply(
        **{
            "session_id": credentials.session_id,
            "message_id": uuid.uuid4(),
            "client_message_id": mid,
            "request_id": "r1",
            "decision": Decision.ANSWERED,
            "answer": "An answer",
            "generation_method": "approved_faq",
            "sources": [SOURCE],
            "created_at": NOW,
        }
        | fields
    )


class FakeClient:
    """Records calls; ``script`` maps a method name to a list of results or errors."""

    def __init__(self, **script: list[Any]) -> None:
        self.script = script
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def _next(self, name: str, *args: Any) -> Any:
        self.calls.append((name, args))
        results = self.script.get(name)
        result = results.pop(0) if results else None
        if isinstance(result, Exception):
            raise result
        return result

    def create_conversation(self) -> ConversationCreated:
        return self._next("create") or created()

    def send_message(self, credentials: Credentials, text: str, mid: uuid.UUID) -> ChatReply:
        return self._next("send", credentials, text, mid) or reply_for(
            credentials, mid, answer=f"Answer to {text}"
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

    def sent_ids(self) -> list[uuid.UUID]:
        return [args[2] for name, args in self.calls if name == "send"]


def opened(client: FakeClient | None = None) -> ChatState:
    """A page that has just been opened (and has started its conversation)."""
    state = ChatState()
    actions.start_conversation(state, client or FakeClient())  # type: ignore[arg-type]
    return state


def send(state: ChatState, client: FakeClient, text: str = "Hello") -> None:
    state.queue_message(text, max_chars=4000)
    actions.send_pending(state, client)  # type: ignore[arg-type]


# --- Task 5: state basics -------------------------------------------------------------


def test_state_is_created_once_per_session_store() -> None:
    store: dict[str, Any] = {}
    state = get_chat_state(store)
    assert store[STATE_KEY] is state
    assert get_chat_state(store) is state  # a rerun gets the same object
    assert get_chat_state({}) is not state  # another browser session gets its own


def test_the_token_is_masked_in_the_state() -> None:
    state = opened()
    assert TOKEN not in repr(state) and TOKEN not in str(state)


# --- Task 6: conversation initialisation ----------------------------------------------


def test_opening_the_page_starts_one_conversation() -> None:
    client = FakeClient()
    state = ChatState()
    assert state.should_auto_start
    actions.start_conversation(state, client)  # type: ignore[arg-type]
    assert state.credentials is not None
    assert state.conversation_status is ConversationStatus.ACTIVE
    assert state.expires_at == NOW + timedelta(hours=1)
    assert not state.should_auto_start  # a rerun creates nothing
    actions.start_conversation(state, client)  # type: ignore[arg-type]
    assert client.names() == ["create"]  # never replaced


def test_a_failed_start_is_shown_once_and_can_be_retried() -> None:
    client = FakeClient(create=[ApiError(ErrorKind.BACKEND_UNREACHABLE)])
    state = opened(client)
    assert state.credentials is None
    assert state.last_error == ErrorNotice(kind=ErrorKind.BACKEND_UNREACHABLE)
    assert state.can_retry_start and not state.should_auto_start  # no automatic retry
    actions.start_conversation(state, client)  # type: ignore[arg-type] - "Try again"
    assert state.credentials is not None and state.last_error is None
    assert not state.can_retry_start


def test_a_message_typed_after_a_failed_start_starts_the_conversation() -> None:
    client = FakeClient(create=[ApiError(ErrorKind.SERVICE_UNAVAILABLE)])
    state = opened(client)
    send(state, client)
    assert client.names() == ["create", "create", "send"]
    assert [m.role for m in state.messages] == ["user", "assistant"]


# --- Task 7: message submission -------------------------------------------------------


def test_queueing_a_message_gives_it_a_new_id_and_blocks_another() -> None:
    state = opened()
    first = state.queue_message("  Hello  ", max_chars=4000)
    assert first is not None and first.text == "Hello"
    assert state.is_sending and not state.can_send
    assert state.messages[-1].delivery is Delivery.PENDING
    assert state.messages[-1].message_id == str(first.client_message_id)
    assert state.queue_message("Another", max_chars=4000) is None  # no double submit


def test_blank_messages_are_ignored() -> None:
    state = opened()
    assert state.queue_message(" \n\t ", max_chars=4000) is None
    assert state.messages == [] and not state.is_sending and state.last_error is None


def test_a_message_over_the_limit_goes_back_into_the_input() -> None:
    state = opened()
    assert state.queue_message("x" * 101, max_chars=100) is None
    assert state.messages == [] and not state.is_sending
    assert state.take_draft() == "x" * 101
    assert state.take_draft() is None  # restored once
    assert error_text(state.last_error) == (  # type: ignore[arg-type]
        "Your message is too long (at most 100 characters). Please shorten it."
    )


def test_a_reply_is_recorded_with_its_sources() -> None:
    client = FakeClient()
    state = opened(client)
    send(state, client)
    assert [m.role for m in state.messages] == ["user", "assistant"]
    assert state.messages[0].delivery is Delivery.SENT
    assert state.messages[1].sources == (SOURCE,)
    assert state.request_status is RequestStatus.IDLE and state.pending is None


def test_each_message_gets_its_own_id() -> None:
    client = FakeClient()
    state = opened(client)
    send(state, client, "One")
    send(state, client, "Two")
    assert len(set(client.sent_ids())) == 2


def test_nothing_is_sent_while_idle() -> None:
    client = FakeClient()
    state = opened(client)
    actions.send_pending(state, client)  # type: ignore[arg-type]
    assert client.names() == ["create"]


@pytest.mark.parametrize(
    "kind",
    [ErrorKind.TIMEOUT, ErrorKind.BUSY, ErrorKind.NETWORK_ERROR, ErrorKind.SERVICE_UNAVAILABLE],
)
def test_an_uncertain_failure_is_retried_with_the_same_id(kind: ErrorKind) -> None:
    client = FakeClient(send=[ApiError(kind)])
    state = opened(client)
    send(state, client)
    assert state.request_status is RequestStatus.FAILED and state.can_retry
    assert state.messages[-1].delivery is Delivery.PENDING  # still on the page

    assert state.retry_pending()
    actions.send_pending(state, client)  # type: ignore[arg-type]
    first, retry = client.sent_ids()
    assert first == retry
    assert state.request_status is RequestStatus.IDLE and state.last_error is None
    assert [m.role for m in state.messages] == ["user", "assistant"]


def test_a_replayed_reply_is_not_shown_twice() -> None:
    state = opened()
    credentials = state.credentials
    assert credentials is not None
    pending = state.queue_message("Hello", max_chars=4000)
    assert pending is not None
    reply = reply_for(credentials, pending.client_message_id)
    state.record_reply(reply)
    # The same reply again (e.g. a retry after the history was refreshed).
    state.queue_message("Hello", max_chars=4000)
    state.record_reply(reply.model_copy(update={"replayed": True}))
    assert [m.role for m in state.messages].count("assistant") == 1


def test_a_refused_message_goes_back_into_the_input() -> None:
    error = ApiError(
        ErrorKind.INVALID_MESSAGE, validation_types=("string_too_long",), max_length=1000
    )
    client = FakeClient(send=[error])
    state = opened(client)
    send(state, client, "A long message")
    assert state.messages == []  # not stored by the backend, so not shown as sent
    assert state.take_draft() == "A long message"
    assert not state.can_retry and state.can_send
    assert state.backend_max_chars == 1000
    assert state.max_chars(4000) == 1000  # the input now respects the backend's limit


def test_typing_a_new_message_gives_up_the_failed_one() -> None:
    client = FakeClient(send=[ApiError(ErrorKind.SERVICE_UNAVAILABLE)])
    state = opened(client)
    send(state, client, "Lost")
    send(state, client, "Next")
    assert [m.delivery for m in state.messages if m.role == "user"] == [
        Delivery.NOT_SENT,
        Delivery.SENT,
    ]


def test_an_expired_conversation_keeps_its_credentials_for_deletion() -> None:
    client = FakeClient(send=[None, ApiError(ErrorKind.SESSION_EXPIRED)])
    state = opened(client)
    send(state, client, "One")
    send(state, client, "Two")
    assert state.conversation_status is ConversationStatus.EXPIRED
    assert state.credentials is not None  # DELETE still works after expiry
    assert state.has_ended and not state.can_send and not state.can_retry
    assert len(state.messages) == 2  # the history stays on the page
    assert state.take_draft() == "Two"  # ready for a new conversation


def test_an_unknown_conversation_loses_its_credentials() -> None:
    client = FakeClient(send=[ApiError(ErrorKind.SESSION_NOT_FOUND)])
    state = opened(client)
    send(state, client)
    assert state.credentials is None and state.has_ended


def test_a_conversation_past_its_expiry_time_is_marked_expired() -> None:
    state = opened()
    state.check_expiry(now=NOW + timedelta(minutes=59))
    assert state.conversation_status is ConversationStatus.ACTIVE
    state.check_expiry(now=NOW + timedelta(hours=1))
    assert state.conversation_status is ConversationStatus.EXPIRED
    assert state.credentials is not None


def test_a_closed_conversation_keeps_its_credentials_for_reading() -> None:
    client = FakeClient(send=[ApiError(ErrorKind.SESSION_CLOSED)])
    state = opened(client)
    send(state, client)
    assert state.conversation_status is ConversationStatus.CLOSED
    assert state.credentials is not None and state.has_ended


# --- Task 10: sidebar actions ---------------------------------------------------------


def test_a_new_conversation_is_created_before_the_old_one_is_closed() -> None:
    client = FakeClient()
    state = opened(client)
    old = state.credentials
    send(state, client)
    assert actions.start_new_conversation(state, client)  # type: ignore[arg-type]
    assert client.names()[-2:] == ["create", "close"]
    assert client.calls[-1][1] == (old,)  # the old one is closed
    assert state.credentials is not None and state.credentials != old
    assert state.messages == [] and state.conversation_status is ConversationStatus.ACTIVE


def test_the_current_conversation_is_kept_if_a_new_one_cannot_be_created() -> None:
    client = FakeClient()
    state = opened(client)
    send(state, client)
    old, messages = state.credentials, list(state.messages)
    client.script["create"] = [ApiError(ErrorKind.RATE_LIMITED, retry_after=30)]
    assert not actions.start_new_conversation(state, client)  # type: ignore[arg-type]
    assert state.credentials == old and state.messages == messages
    assert state.last_error is not None and state.last_error.kind is ErrorKind.RATE_LIMITED
    assert state.can_send and not state.can_retry_start
    assert "close" not in client.names()


def test_a_new_conversation_starts_even_if_closing_the_old_one_fails() -> None:
    client = FakeClient(close=[ApiError(ErrorKind.SERVICE_UNAVAILABLE)])
    state = opened(client)
    send(state, client)
    assert actions.start_new_conversation(state, client)  # type: ignore[arg-type]
    assert state.messages == [] and state.last_error is None


def test_an_expired_conversation_is_not_closed_when_replaced() -> None:
    client = FakeClient()
    state = opened(client)
    state.check_expiry(now=NOW + timedelta(hours=2))
    actions.start_new_conversation(state, client)  # type: ignore[arg-type]
    assert client.names() == ["create", "create"]


def test_closing_keeps_the_messages_and_stops_input() -> None:
    client = FakeClient()
    state = opened(client)
    send(state, client)
    actions.close_conversation(state, client)  # type: ignore[arg-type]
    assert state.conversation_status is ConversationStatus.CLOSED
    assert len(state.messages) == 2 and not state.can_send


def test_deleting_calls_the_backend_then_clears_credentials() -> None:
    client = FakeClient()
    state = opened(client)
    send(state, client)
    credentials = state.credentials
    assert actions.delete_conversation(state, client)  # type: ignore[arg-type]
    assert client.calls[-1] == ("delete", (credentials,))
    assert state.credentials is None and state.messages == []
    assert state.conversation_status is None
    assert not state.should_auto_start  # no new conversation until the customer asks


def test_an_expired_conversation_can_still_be_deleted() -> None:
    client = FakeClient()
    state = opened(client)
    state.check_expiry(now=NOW + timedelta(hours=2))
    assert actions.delete_conversation(state, client)  # type: ignore[arg-type]
    assert client.names()[-1] == "delete" and state.credentials is None


def test_deleting_a_conversation_the_backend_no_longer_has_still_clears_it() -> None:
    client = FakeClient(delete=[ApiError(ErrorKind.SESSION_NOT_FOUND)])
    state = opened(client)
    assert actions.delete_conversation(state, client)  # type: ignore[arg-type]
    assert state.credentials is None


def test_a_failed_delete_keeps_the_conversation_to_try_again() -> None:
    client = FakeClient(delete=[ApiError(ErrorKind.BACKEND_UNREACHABLE)])
    state = opened(client)
    send(state, client)
    assert not actions.delete_conversation(state, client)  # type: ignore[arg-type]
    assert state.credentials is not None and len(state.messages) == 2
    assert state.last_error == ErrorNotice(kind=ErrorKind.BACKEND_UNREACHABLE)


def test_reloading_shows_the_backends_history() -> None:
    client = FakeClient()
    state = opened(client)
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
            ("assistant", "Answer", Decision.ANSWERED, ["faq_027", "faq_999"]),
        ]
    ]
    client.script["history"] = [
        ConversationHistory(status=ConversationStatus.ACTIVE, messages=stored, complete=True)
    ]
    actions.reload_history(state, client)  # type: ignore[arg-type]
    assert [m.content for m in state.messages] == ["Hello", "Answer"]
    # faq_027's question is known from this page's reply; faq_999's is not invented.
    assert state.messages[1].sources == (SOURCE,)


# --- Task 9: sources and formatting ---------------------------------------------------


def test_sources_keep_the_backend_order_without_duplicates() -> None:
    a = SourceReference(faq_id="faq_2", question="What is your return policy?")
    b = SourceReference(faq_id="faq_1", question="How long do refunds take?")
    assert unique_sources([a, b, a]) == [a, b]
    assert source_labels([a, b, a]) == [
        ("What is your return policy?", None),
        ("How long do refunds take?", None),
    ]


def test_faq_ids_are_shown_only_to_tell_identical_titles_apart() -> None:
    a = SourceReference(faq_id="faq_1", question="Delivery times")
    b = SourceReference(faq_id="faq_2", question="delivery times ")
    assert source_labels([a, b]) == [("Delivery times", "faq_1"), ("delivery times ", "faq_2")]
    assert source_labels([]) == []


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
