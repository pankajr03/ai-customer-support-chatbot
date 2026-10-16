"""Conversation workflow through the frontend's client and page, on the real backend."""

import uuid
from collections.abc import Callable

import pytest
from streamlit.testing.v1 import AppTest

from frontend import actions
from frontend.api_client import ApiError, ConversationStatus, Decision, ErrorKind
from frontend.state import STATE_KEY, ChatState
from tests.integration.conftest import (
    CONVERSATIONS,
    EXACT,
    FOLLOW_UP,
    OUT_OF_SCOPE,
    Stack,
    say,
)


def test_first_message_follow_up_and_history(stack: Stack) -> None:
    credentials = stack.start()

    first = stack.api.send_message(credentials, EXACT, uuid.uuid4())
    follow_up = stack.api.send_message(credentials, FOLLOW_UP, uuid.uuid4())
    history = stack.api.get_history(credentials)

    assert first.decision is Decision.ANSWERED
    # "How long does it take?" has no topic: resolved from the previous turn.
    assert follow_up.decision is Decision.ANSWERED
    assert [s.faq_id for s in follow_up.sources] == ["faq_107"]
    assert [(m.role, m.content) for m in history.messages] == [
        ("user", EXACT),
        ("assistant", first.answer),
        ("user", FOLLOW_UP),
        ("assistant", follow_up.answer),
    ]
    assert history.status is ConversationStatus.ACTIVE and history.complete


def test_a_follow_up_without_history_is_not_guessed(stack: Stack) -> None:
    credentials = stack.start()
    reply = stack.api.send_message(credentials, FOLLOW_UP, uuid.uuid4())
    assert reply.decision is Decision.NO_MATCH and reply.sources == []


def test_new_conversation_closes_the_old_one_and_starts_empty(stack: Stack) -> None:
    state = ChatState()
    actions.start_conversation(state, stack.api)
    old = state.credentials
    assert old is not None
    state.queue_message(EXACT, max_chars=4000)
    actions.send_pending(state, stack.api)

    assert actions.start_new_conversation(state, stack.api)

    assert state.credentials is not None and state.credentials != old
    assert state.messages == [] and state.conversation_status is ConversationStatus.ACTIVE
    assert stack.api.get_conversation(old).status is ConversationStatus.CLOSED  # not deleted
    assert stack.api.get_history(old).total_messages == 2


def test_closing_stops_new_messages_but_keeps_history(stack: Stack) -> None:
    credentials = stack.start()
    stack.api.send_message(credentials, EXACT, uuid.uuid4())

    assert stack.api.close_conversation(credentials).status is ConversationStatus.CLOSED
    with pytest.raises(ApiError) as caught:
        stack.api.send_message(credentials, OUT_OF_SCOPE, uuid.uuid4())

    assert caught.value.kind is ErrorKind.SESSION_CLOSED and not caught.value.retryable
    assert stack.api.get_history(credentials).total_messages == 2
    assert stack.api.close_conversation(credentials).status is ConversationStatus.CLOSED


def test_deletion_removes_everything_and_the_token_stops_working(stack: Stack) -> None:
    credentials = stack.start()
    stack.api.send_message(credentials, EXACT, uuid.uuid4())

    stack.api.delete_conversation(credentials)

    for call in (stack.api.get_history, stack.api.get_conversation):
        with pytest.raises(ApiError) as caught:
            call(credentials)
        assert caught.value.kind is ErrorKind.SESSION_NOT_FOUND
    with stack.database.read() as session:
        from sqlalchemy import text

        rows = session.execute(text("SELECT count(*) FROM conversation_messages")).scalar()
    assert rows == 0


def test_an_expired_session_stops_chat_but_can_still_be_deleted(stack: Stack) -> None:
    credentials = stack.start()
    stack.sql(
        "UPDATE conversation_sessions SET created_at = '2020-01-01 00:00:00', "
        "updated_at = '2020-01-01 00:00:00', expires_at = '2020-01-01 01:00:00'"
    )

    for call in (
        lambda: stack.api.send_message(credentials, EXACT, uuid.uuid4()),
        lambda: stack.api.get_history(credentials),
    ):
        with pytest.raises(ApiError) as caught:
            call()
        assert caught.value.kind is ErrorKind.SESSION_EXPIRED and caught.value.ends_conversation
    stack.api.delete_conversation(credentials)  # still allowed


def test_an_expired_session_on_the_page_offers_a_new_conversation(
    stack: Stack, open_page: Callable[..., AppTest]
) -> None:
    page = open_page(stack)
    stack.sql(
        "UPDATE conversation_sessions SET created_at = '2020-01-01 00:00:00', "
        "updated_at = '2020-01-01 00:00:00', expires_at = '2020-01-01 01:00:00'"
    )
    say(page, EXACT)

    state: ChatState = page.session_state[STATE_KEY]
    assert state.conversation_status is ConversationStatus.EXPIRED
    assert "This conversation has expired" in page.error[0].value
    assert page.chat_input[0].disabled
    next(b for b in page.button if b.label == "Start a new conversation").click().run()
    state = page.session_state[STATE_KEY]
    assert state.conversation_status is ConversationStatus.ACTIVE
    assert page.chat_input[0].proto.value == EXACT  # the message is ready to send again
    assert stack.faults.count("POST", CONVERSATIONS) == 2
