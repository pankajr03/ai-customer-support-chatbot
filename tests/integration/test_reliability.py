"""Reliability: timeouts, duplicates, conflicts and broken dependencies, end to end.

``FaultInjector`` decides what happens to each request, so every test knows what the
backend really stored, and checks that the frontend ends up showing exactly that.
"""

import uuid
from collections.abc import Callable
from typing import Any

import pytest

from frontend import actions
from frontend.api_client import ApiError, BackendStatus, Decision, ErrorKind
from frontend.state import ChatState, Delivery, RequestStatus
from tests.integration.conftest import CONVERSATIONS, EXACT, OUT_OF_SCOPE, Stack

LIMIT = 4000


def opened(stack: Stack) -> ChatState:
    state = ChatState()
    actions.start_conversation(state, stack.api)
    assert state.credentials is not None
    return state


def send(state: ChatState, stack: Stack, text: str) -> None:
    assert state.queue_message(text, max_chars=LIMIT) is not None
    actions.send_pending(state, stack.api)


def stored(state: ChatState, stack: Stack) -> list[tuple[str, str]]:
    assert state.credentials is not None
    return [(m.role, m.content) for m in stack.api.get_history(state.credentials).messages]


# --- Timeouts: the outcome is unknown, so the same ID is used again ---------------------


@pytest.mark.parametrize("step", ["lost", "reply_lost"])
def test_a_backend_timeout_is_retried_safely_with_the_same_id(stack: Stack, step: str) -> None:
    state = opened(stack)
    stack.faults.plan = {"/chat": [step]}
    send(state, stack, EXACT)
    assert state.request_status is RequestStatus.FAILED
    assert state.last_error is not None and state.last_error.kind is ErrorKind.TIMEOUT
    assert state.pending is not None
    first_id = state.pending.client_message_id

    assert state.retry_pending()
    actions.send_pending(state, stack.api)

    # Answered once whether or not the first attempt had been stored.
    assert stored(state, stack) == [(m.role, m.content) for m in state.messages]
    assert len(state.messages) == 2
    assert state.messages[0].message_id == str(first_id)


def test_a_timeout_whose_reply_was_stored_is_settled_by_refreshing(stack: Stack) -> None:
    state = opened(stack)
    stack.faults.plan = {"/chat": ["reply_lost"]}
    send(state, stack, EXACT)

    actions.reload_history(state, stack.api)

    assert state.pending is None and state.can_send and state.last_error is None
    assert [m.delivery for m in state.messages] == [Delivery.SENT, Delivery.SENT]
    assert stack.faults.count("POST", "/chat") == 1


# --- Duplicates and conflicts -----------------------------------------------------------


def test_a_duplicate_submission_is_answered_once(stack: Stack) -> None:
    state = opened(stack)
    assert state.queue_message(EXACT, max_chars=LIMIT) is not None
    # While it is being sent, the page refuses any other submission.
    assert state.queue_message(EXACT, max_chars=LIMIT) is None
    assert state.queue_message(OUT_OF_SCOPE, max_chars=LIMIT) is None
    actions.send_pending(state, stack.api)
    actions.send_pending(state, stack.api)  # a second run does nothing

    assert len(stored(state, stack)) == 2
    assert stack.faults.count("POST", "/chat") == 1


def test_the_same_message_sent_twice_by_a_client_is_replayed(stack: Stack) -> None:
    credentials = stack.start()
    message_id = uuid.uuid4()
    replies = [stack.api.send_message(credentials, EXACT, message_id) for _ in range(3)]
    assert [r.replayed for r in replies] == [False, True, True]
    assert len({r.message_id for r in replies}) == 1
    assert stack.api.get_history(credentials).total_messages == 2


def test_a_request_conflict_while_processing_is_bounded(stack: Stack) -> None:
    state = opened(stack)
    stack.sql(
        "UPDATE conversation_sessions SET processing_key = 'other', "
        "processing_until = '2099-01-01 00:00:00'"
    )
    send(state, stack, EXACT)

    assert state.last_error is not None and state.last_error.kind is ErrorKind.BUSY
    # The first try and two automatic retries after the 2-second Retry-After: no loop.
    assert state.can_retry and stack.faults.count("POST", "/chat") == 3
    assert stored(state, stack) == []


def test_a_short_conflict_is_retried_automatically_a_few_times(
    make_stack: Callable[..., Stack],
) -> None:
    stack = make_stack()
    state = opened(stack)
    stack.sql(
        "UPDATE conversation_sessions SET processing_key = 'other', "
        "processing_until = '2099-01-01 00:00:00'"
    )
    attempts: list[float] = []
    stack.api._retry_wait = lambda error, attempt: (  # type: ignore[method-assign]
        attempts.append(attempt) or (0.0 if attempt < 2 else None)
    )
    send(state, stack, EXACT)
    assert stack.faults.count("POST", "/chat") == 3  # the first try and two retries
    assert state.can_retry


# --- Broken dependencies ----------------------------------------------------------------


def test_retrieval_unavailable_is_a_stored_reply_not_an_error(
    make_stack: Callable[..., Stack],
) -> None:
    def broken(name: str) -> Any:
        raise OSError("no model")

    stack = make_stack(loader=broken)
    state = opened(stack)
    send(state, stack, "Is there anything about orders?")

    assert state.last_error is None
    assert state.messages[-1].decision is Decision.RETRIEVAL_UNAVAILABLE
    assert len(stored(state, stack)) == 2
    assert stack.api.check_readiness().checks.semantic_search == "unavailable"
    assert stack.api.backend_status() is BackendStatus.LIMITED


def test_a_database_failure_is_reported_and_keeps_the_message(stack: Stack) -> None:
    state = opened(stack)
    stack.sql("DROP TABLE conversation_messages")
    send(state, stack, EXACT)

    assert state.last_error is not None
    assert state.last_error.kind is ErrorKind.SERVICE_UNAVAILABLE and state.can_retry
    assert state.messages[0].content == EXACT  # kept for the retry
    assert stack.api.backend_status() is BackendStatus.UNAVAILABLE  # /ready says 503
    stack.sql("DROP TABLE conversation_sessions")  # now nothing can be created either
    with pytest.raises(ApiError) as caught:
        stack.api.create_conversation()
    assert caught.value.kind is ErrorKind.SERVICE_UNAVAILABLE


# --- Broken or older replies ------------------------------------------------------------


def test_a_malformed_reply_is_a_contract_error_and_retrying_is_safe(stack: Stack) -> None:
    state = opened(stack)
    stack.faults.plan = {"/chat": ["malformed"]}
    send(state, stack, EXACT)

    assert state.last_error is not None
    assert state.last_error.kind is ErrorKind.INVALID_RESPONSE and state.can_retry
    state.retry_pending()
    actions.send_pending(state, stack.api)  # same ID: the stored reply comes back
    assert len(state.messages) == 2 and len(stored(state, stack)) == 2


def test_a_malformed_history_leaves_the_page_as_it_was(stack: Stack) -> None:
    state = opened(stack)
    send(state, stack, EXACT)
    before = list(state.messages)
    stack.faults.plan = {"/messages": ["malformed"]}

    actions.reload_history(state, stack.api)

    assert state.messages == before
    assert state.last_error is not None and state.last_error.kind is ErrorKind.INVALID_RESPONSE


def test_replies_without_optional_fields_are_still_understood(stack: Stack) -> None:
    state = opened(stack)
    # Null fields (usage, ...) and the Stage 9 history additions left out, as an older
    # backend would send them.
    stack.faults.plan = {"/chat": ["strip_optional"], "/messages": ["strip_optional"]}
    send(state, stack, EXACT)
    assert state.messages[-1].decision is Decision.ANSWERED
    assert [s.faq_id for s in state.messages[-1].sources] == ["faq_107"]

    actions.reload_history(state, stack.api)  # history without client_message_id/sources
    assert [(m.role, m.delivery) for m in state.messages] == [
        ("user", Delivery.SENT),
        ("assistant", Delivery.SENT),
    ]
    assert [s.faq_id for s in state.messages[-1].sources] == ["faq_107"]  # kept from the page


def test_readiness_reports_storage_failure_without_details(stack: Stack) -> None:
    stack.sql("DROP TABLE conversation_sessions")
    response = stack.http.get("/api/v1/ready")
    assert response.status_code == 503
    assert response.json()["checks"]["conversation_storage"] == "unavailable"
    assert "conversation_sessions" not in response.text
    assert stack.faults.count("GET", "/ready") == 1
    assert stack.faults.count("POST", CONVERSATIONS) == 0


def test_each_turn_logs_its_stage_timings_without_text(
    stack: Stack, caplog: pytest.LogCaptureFixture
) -> None:
    import logging
    import re

    credentials = stack.start()
    with caplog.at_level(logging.DEBUG):
        stack.api.send_message(credentials, EXACT, uuid.uuid4())

    turn = next(
        r.getMessage() for r in caplog.records if "event=conversation_turn" in r.getMessage()
    )
    for stage in ("history_ms", "rewrite_ms", "pipeline_ms", "store_ms", "turn_ms"):
        assert re.search(rf"\b{stage}=\d+\.\d\b", turn), stage
    search = next(r.getMessage() for r in caplog.records if "event=hybrid_search" in r.getMessage())
    assert re.search(r"keyword_ms=\d+\.\d semantic_ms=\d+\.\d", search)
    assert EXACT not in caplog.text and "password" not in turn + search
