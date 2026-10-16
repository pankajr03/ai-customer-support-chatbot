"""Page state against backend state (Stage 9, Tasks 6 and 7).

The backend's stored history is authoritative for what was saved; the page's own list
also holds what was not saved yet (pending, failed, abandoned customer messages). These
tests run ``ChatState`` and ``frontend.actions`` against the real backend in-process,
with a transport that loses requests or replies on purpose, so each case has a known
"what the backend really stored".

Reconciliation strategy (see frontend/README.md, "Conversation history"):

- stored messages come from the backend, in its order, matched to the page's own by
  stable IDs (``client_message_id`` for customer messages, ``message_id`` for replies);
- a customer message the backend does not have is never dropped: it stays on the page,
  marked, with **Send again**, which reuses its ``client_message_id``;
- a pending message the backend turns out to have stored is resolved, not resent.
"""

import importlib
import uuid
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from frontend import actions
from frontend.api_client import ApiError, ChatApiClient, ConversationStatus, ErrorKind
from frontend.state import ChatState, Delivery, RequestStatus
from tests.test_api_workflows import RESET, SHIPPING, UNKNOWN

LIMIT = 4000


class Flaky(httpx.BaseTransport):
    """Wraps the backend's transport. Each chat request takes the next step from
    ``plan``: "ok", "lost" (never reaches the backend), "reply_lost" (stored and
    answered, but the reply never arrives), or "busy" (a 409 as if still processing)."""

    def __init__(self, inner: httpx.BaseTransport) -> None:
        self.inner = inner
        self.plan: list[str] = []
        self.chat_requests: list[str] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if not request.url.path.endswith("/chat") or not self.plan:
            return self.inner.handle_request(request)
        step = self.plan.pop(0)
        self.chat_requests.append(step)
        if step == "lost":
            raise httpx.ReadTimeout("lost on the way", request=request)
        if step == "busy":
            body = {"error": {"code": "REQUEST_IN_PROGRESS", "message": "m", "request_id": "r"}}
            # The same HTTP library as the request (Starlette's TestClient uses a fork).
            library = importlib.import_module(type(request).__module__.split(".")[0])
            return library.Response(409, json=body, headers={"Retry-After": "60"})
        response = self.inner.handle_request(request)
        if step == "reply_lost":
            response.read()
            raise httpx.ReadTimeout("reply lost", request=request)
        return response


@pytest.fixture
def setup(make_backend: Callable[..., TestClient]) -> tuple[ChatState, ChatApiClient, Flaky]:
    backend = make_backend()
    flaky = Flaky(backend._transport)
    backend._transport = flaky
    client = ChatApiClient(http_client=backend, sleep=lambda seconds: None)
    state = ChatState()
    actions.start_conversation(state, client)
    return state, client, flaky


def say(state: ChatState, client: ChatApiClient, text: str) -> None:
    assert state.queue_message(text, max_chars=LIMIT) is not None
    actions.send_pending(state, client)


def stored(state: ChatState, client: ChatApiClient) -> list[tuple[str, str]]:
    assert state.credentials is not None
    return [(m.role, m.content) for m in client.get_history(state.credentials).messages]


def shown(state: ChatState) -> list[tuple[str, str, Delivery]]:
    return [(m.role, m.content, m.delivery) for m in state.messages]


def customer_ids(state: ChatState) -> list[str]:
    return [m.message_id for m in state.messages if m.role == "user"]


# --- Reconciliation: the backend is authoritative, nothing typed is lost ----------------


def test_an_abandoned_message_the_backend_never_got_is_kept(setup: Any) -> None:
    state, client, flaky = setup
    flaky.plan = ["lost"]
    say(state, client, RESET)  # timed out: maybe stored, maybe not
    say(state, client, SHIPPING)  # the customer moves on; the page re-reads the history

    assert stored(state, client) == [("user", SHIPPING), ("assistant", state.messages[1].content)]
    # Not silently dropped: still on the page, after the stored conversation, marked,
    # and it can be sent again.
    assert shown(state)[-1] == ("user", RESET, Delivery.UNCERTAIN)
    assert [m.role for m in state.messages] == ["user", "assistant", "user"]


def test_refreshing_keeps_messages_that_were_not_sent(setup: Any) -> None:
    state, client, flaky = setup
    flaky.plan = ["lost"]
    say(state, client, RESET)
    say(state, client, SHIPPING)
    actions.reload_history(state, client)
    actions.reload_history(state, client)  # repeatable, no duplicates
    assert [c for r, c, _ in shown(state) if r == "user"] == [SHIPPING, RESET]
    assert len(state.messages) == 3


def test_a_stored_message_whose_reply_was_lost_is_resolved_by_a_refresh(setup: Any) -> None:
    state, client, flaky = setup
    flaky.plan = ["reply_lost"]
    say(state, client, RESET)
    assert state.request_status is RequestStatus.FAILED and state.pending is not None
    pending_id = str(state.pending.client_message_id)

    actions.reload_history(state, client)

    # The backend has it: the pending message is settled, shown once, with its reply.
    assert state.pending is None and state.last_error is None and state.can_send
    assert [(r, d) for r, _, d in shown(state)] == [
        ("user", Delivery.SENT),
        ("assistant", Delivery.SENT),
    ]
    assert customer_ids(state) == [pending_id]  # matched by its client_message_id
    assert [s.faq_id for s in state.messages[1].sources] == ["faq_107"]
    assert flaky.chat_requests == ["reply_lost"]  # not sent again


def test_a_stored_message_whose_reply_was_lost_is_replayed_by_try_again(setup: Any) -> None:
    state, client, flaky = setup
    flaky.plan = ["reply_lost"]
    say(state, client, RESET)
    first_id = state.pending.client_message_id

    assert state.retry_pending()
    actions.send_pending(state, client)

    assert stored(state, client) == [(r, c) for r, c, _ in shown(state)]  # answered once
    assert len(state.messages) == 2 and customer_ids(state) == [str(first_id)]


# --- Send again: the same logical submission keeps its ID -------------------------------


def test_send_again_reuses_the_id_and_never_answers_twice(setup: Any) -> None:
    state, client, flaky = setup
    flaky.plan = ["reply_lost"]  # stored and answered; the page doesn't know
    say(state, client, RESET)
    first_id = customer_ids(state)[0]
    say(state, client, UNKNOWN)  # moved on: RESET is "may not have been sent"
    actions.reload_history(state, client)
    # The refresh resolved it: it was stored. Nothing to send again.
    assert all(d is Delivery.SENT for _, _, d in shown(state))
    assert customer_ids(state)[0] == first_id
    assert len(stored(state, client)) == 4


def test_send_again_after_a_lost_request(setup: Any) -> None:
    state, client, flaky = setup
    flaky.plan = ["lost"]
    say(state, client, RESET)
    say(state, client, SHIPPING)
    lost_id = customer_ids(state)[-1]  # kept at the end, after what the backend stored
    assert shown(state)[-1][1] == RESET

    assert state.resend(lost_id) is not None
    assert state.pending is not None and str(state.pending.client_message_id) == lost_id
    actions.send_pending(state, client)

    assert [c for r, c in stored(state, client) if r == "user"] == [SHIPPING, RESET]
    assert [(r, c) for r, c, _ in shown(state)] == stored(state, client)  # same order
    assert all(d is Delivery.SENT for _, _, d in shown(state))


def test_send_again_of_a_message_that_was_stored_after_all_is_a_replay(setup: Any) -> None:
    state, client, flaky = setup
    flaky.plan = ["reply_lost"]
    say(state, client, RESET)
    first_id = customer_ids(state)[0]
    state.queue_message(SHIPPING, max_chars=LIMIT)  # moved on, then the send is lost too
    flaky.plan = ["lost"]
    actions.send_pending(state, client)
    assert state.messages[0].delivery is Delivery.UNCERTAIN

    state.resend(first_id)  # RESET again, same ID: the backend replays its reply
    actions.send_pending(state, client)

    assert [c for r, c in stored(state, client) if r == "user"] == [RESET]  # once
    assert [(r, c) for r, c, d in shown(state) if d is Delivery.SENT] == stored(state, client)
    assert [c for r, c, d in shown(state) if d is not Delivery.SENT] == [SHIPPING]


def test_only_unsent_customer_messages_can_be_sent_again(setup: Any) -> None:
    state, client, _ = setup
    say(state, client, RESET)
    assert state.resend(customer_ids(state)[0]) is None  # already answered
    assert state.resend(state.messages[1].message_id) is None  # a reply
    assert state.resend(str(uuid.uuid4())) is None


# --- Submission: validation, pending state, bounded retries ------------------------------


@pytest.mark.parametrize(
    ("text", "rule"),
    [("hello\x00world", "invalid_characters"), ("\x1b[31mred", "invalid_characters")],
)
def test_invalid_messages_are_refused_before_sending(setup: Any, text: str, rule: str) -> None:
    state, client, flaky = setup
    assert state.queue_message(text, max_chars=LIMIT) is None
    assert state.draft == text and state.messages == []  # back in the input, not lost
    assert state.last_error is not None and rule in state.last_error.validation_types
    assert flaky.chat_requests == []


def test_tabs_and_line_breaks_are_fine(setup: Any) -> None:
    state, client, _ = setup
    say(state, client, "Line one\n\tline two: password?")
    assert state.messages[-1].role == "assistant"


def test_a_message_still_processing_is_retried_a_bounded_number_of_times(
    setup: Any,
) -> None:
    state, client, flaky = setup
    flaky.plan = ["busy", "busy", "busy", "busy"]
    say(state, client, RESET)
    # Retry-After (60 s) is longer than the page waits by itself: no automatic loop.
    assert flaky.chat_requests == ["busy"]
    assert state.request_status is RequestStatus.FAILED and state.can_retry
    assert state.last_error is not None and state.last_error.kind is ErrorKind.BUSY
    assert state.messages[0].delivery is Delivery.PENDING


# --- Ended conversations keep the customer's unsent text ------------------------------


def _fail_pending(state: ChatState, client: ChatApiClient, flaky: Flaky) -> str:
    flaky.plan = ["lost"]
    say(state, client, RESET)
    assert state.can_retry
    return RESET


def test_unsent_text_survives_expiry_and_a_new_conversation(setup: Any) -> None:
    state, client, flaky = setup
    text = _fail_pending(state, client, flaky)
    assert state.expires_at is not None
    state.check_expiry(now=state.expires_at)
    actions.start_new_conversation(state, client)
    assert state.conversation_status is ConversationStatus.ACTIVE
    assert state.take_draft() == text


def test_unsent_text_survives_closing_and_a_new_conversation(setup: Any) -> None:
    state, client, flaky = setup
    text = _fail_pending(state, client, flaky)
    actions.close_conversation(state, client)
    actions.start_new_conversation(state, client)
    assert state.take_draft() == text


def test_unsent_text_survives_starting_a_new_conversation(setup: Any) -> None:
    state, client, flaky = setup
    text = _fail_pending(state, client, flaky)
    actions.start_new_conversation(state, client)
    assert state.take_draft() == text and state.messages == []


def test_unsent_text_survives_deleting_the_conversation(setup: Any) -> None:
    state, client, flaky = setup
    text = _fail_pending(state, client, flaky)
    assert actions.delete_conversation(state, client)
    assert state.credentials is None and state.take_draft() == text


def test_a_conversation_deleted_elsewhere_keeps_the_message(setup: Any) -> None:
    state, client, _ = setup
    assert state.credentials is not None
    client.delete_conversation(state.credentials)  # e.g. from another device
    say(state, client, RESET)
    assert state.last_error is not None
    assert state.last_error.kind is ErrorKind.SESSION_NOT_FOUND
    assert state.credentials is None  # the token is useless now
    actions.start_new_conversation(state, client)
    assert state.take_draft() == RESET


# --- Isolation --------------------------------------------------------------------------


def test_one_session_cannot_read_or_apply_another_sessions_history(
    make_backend: Callable[..., TestClient],
) -> None:
    client = ChatApiClient(http_client=make_backend(), sleep=lambda seconds: None)
    alice, bob = ChatState(), ChatState()
    actions.start_conversation(alice, client)
    actions.start_conversation(bob, client)
    say(alice, client, RESET)
    assert alice.credentials is not None and bob.credentials is not None

    # Bob's state never applies Alice's history, even if handed it.
    assert not bob.replace_history(client.get_history(alice.credentials), bob.credentials)
    assert bob.messages == []
    # Alice's session ID with Bob's token is refused like an unknown conversation.
    mixed = type(alice.credentials)(
        session_id=alice.credentials.session_id, access_token=bob.credentials.access_token
    )
    with pytest.raises(ApiError) as caught:
        client.get_history(mixed)
    assert caught.value.kind is ErrorKind.SESSION_NOT_FOUND
    assert "cst_" not in repr(alice) + str(alice.credentials)  # tokens never printed
