"""Customer chat orchestration (Stage 7, Task 5): ``CustomerChatService`` end to end.

The pipeline steps themselves (retrieval, decision, generation, storage) have their own
tests; these check how the service coordinates them: what is validated before any work,
what reaches search and generation, which replies are stored, and how partial failures
end.
"""

from collections.abc import Callable, Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api.dependencies import get_customer_chat_service
from app.core.config import Settings
from app.database.connection import Database
from app.models.answer import GenerationMethod
from app.models.chat import ChatDecision, CustomerChatRequest
from app.models.generation import GenerationOutcome, GenerationResult
from app.services.answer_decision_service import Decision
from app.services.conversation_service import ConversationNotFoundError
from app.services.customer_chat_service import (
    CustomerChatService,
    CustomerMessageTooLongError,
    search_text,
)
from app.services.faq_service import FAQService
from app.services.semantic_search_service import MAX_QUERY_LENGTH
from tests.fakes import FAQS
from tests.test_chat_contract import FakeGrounded, grounded
from tests.test_conversations_api import BASE, build_app, create
from tests.test_customer_chat_service import NOW, ask, build, reply

RETURN_POLICY = "What is your return policy?"


@pytest.fixture
def database() -> Iterator[Database]:
    db = Database("sqlite://")
    db.init_schema()
    yield db
    db.dispose()


class RecordingGrounded:
    """Wraps the real grounded-answer service and records what it is given."""

    def __init__(self, real: Any) -> None:
        self._real = real
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def answer(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append((args, kwargs))
        return self._real.answer(*args, **kwargs)


# --- 1. Validation comes first -----------------------------------------------------------


def test_a_too_long_message_is_refused_before_any_work(database: Database) -> None:
    s = build(database, max_message_chars=100)

    with pytest.raises(CustomerMessageTooLongError) as caught:
        s.send("x" * 101)

    assert (caught.value.length, caught.value.limit) == (101, 100)
    assert s.hybrid.calls == [] and s.stored() == []
    assert s.send("x" * 100).response.decision is ChatDecision.NO_MATCH  # no lease was held


def test_the_length_check_does_not_need_a_valid_token(database: Database) -> None:
    s = build(database, max_message_chars=100)

    with pytest.raises(CustomerMessageTooLongError):  # nothing is looked up
        s.chat.chat(s.created.session.session_id, "cst_wrong", ask("x" * 101), now=NOW)
    with pytest.raises(ConversationNotFoundError):
        s.chat.chat(s.created.session.session_id, "cst_wrong", ask("x" * 100), now=NOW)


def test_the_service_rejects_a_non_positive_limit(database: Database) -> None:
    with pytest.raises(ValueError, match="max_message_chars"):
        build(database, max_message_chars=0)


# --- 5. What is searched, and what is stored ---------------------------------------------


def test_line_breaks_are_stored_but_not_searched(database: Database) -> None:
    s = build(database)
    message = "What is your\nreturn   policy?"

    turn = s.send(message)

    assert s.stored()[0].content == message  # the customer's text, unchanged
    assert s.hybrid.calls[0]["query"] == "What is your return policy?"
    assert turn.response.decision is ChatDecision.ANSWERED


def test_a_long_message_is_stored_whole_and_searched_up_to_the_retrieval_limit(
    database: Database,
) -> None:
    s = build(database)
    message = (RETURN_POLICY + " " + "Some more detail about my situation. " * 90).strip()
    assert MAX_QUERY_LENGTH < len(message) <= 4000

    turn = s.send(message)

    assert s.stored()[0].content == message
    query = s.hybrid.calls[0]["query"]
    assert len(query) <= MAX_QUERY_LENGTH
    assert " ".join(message.split()).startswith(query)
    assert turn.response.decision is ChatDecision.ANSWERED


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("  a\n b\t c  ", "a b c"),
        ("x" * 9, "x" * 9),
        ("word " * 3, "word word"),  # 14 characters collapsed: the last word is dropped
        ("aaaa bbbb cccc", "aaaa bbbb"),  # cut at the last word boundary
        ("aaaa bbbb", "aaaa bbbb"),  # exactly the limit
        ("abcdefghijkl", "abcdefghi"),  # one long word: cut hard
    ],
)
def test_search_text(text: str, expected: str) -> None:
    assert search_text(text, limit=9) == expected


def test_search_text_never_exceeds_the_limit() -> None:
    for text in ("a " * 600, "ab " * 400, "x" * 1500, "abc defghij " * 100):
        assert len(search_text(text)) <= MAX_QUERY_LENGTH


# --- History is context, never evidence ---------------------------------------------------


def test_generation_gets_the_query_only_never_the_history(database: Database) -> None:
    s = build(database)
    recording = RecordingGrounded(s.chat._grounded)
    s.chat._grounded = recording  # type: ignore[assignment]
    s.send(RETURN_POLICY)

    s.send("What about after 30 days?")

    args, kwargs = recording.calls[-1]
    assert len(args) == 1 and set(kwargs) == {"category", "safety_identifier"}
    assert FAQS["faq_106"].answer not in args[0]  # the earlier reply is not passed on


# --- 7. Replies are validated before they are stored -------------------------------------


def test_a_reply_that_fails_validation_becomes_the_safe_message(database: Database) -> None:
    # Stage 5 claims an answer, but its only source is not in the evidence.
    unsupported = GenerationResult(
        answer="An answer with no approved source.",
        is_answerable=True,
        source_faq_ids=("faq_999",),
        outcome=GenerationOutcome.GENERATED,
    )
    s = build(
        database,
        grounded=FakeGrounded(grounded(Decision.ANSWERED, unsupported, None)),
        fallback_message="Please contact our support team.",
    )

    response = s.send(RETURN_POLICY).response

    assert response.decision is ChatDecision.NO_MATCH
    assert response.answer == "Please contact our support team."
    assert response.generation_method is GenerationMethod.FALLBACK
    assert response.sources == []
    stored = s.stored()[1]
    assert (stored.decision, stored.content) == (Decision.NO_MATCH, response.answer)


def test_a_claimed_action_is_never_shown(database: Database) -> None:
    """The assistant cannot act: an LLM reply saying it did falls back to the FAQ."""
    s = build(database, reply("I've processed your refund already.", "faq_106"), llm_enabled=True)

    response = s.send(RETURN_POLICY).response

    assert len(s.client.calls) == 1  # the LLM was asked ...
    assert response.generation_method is GenerationMethod.APPROVED_FAQ  # ... and overruled
    assert response.answer == FAQS["faq_106"].answer
    assert "processed your refund" not in s.stored()[1].content


# --- 2. Idempotency -----------------------------------------------------------------------


def test_a_retry_with_different_spacing_is_the_same_message(database: Database) -> None:
    s = build(database)
    first = s.send(RETURN_POLICY, idempotency_key="req-1")

    again = s.send("  What is your\nreturn  policy?", idempotency_key="req-1")

    assert again.replayed
    assert again.response.message_id == first.response.message_id
    assert len(s.stored()) == 2


def test_client_message_id_is_the_retry_key(database: Database) -> None:
    s = build(database)
    sid, token = s.created.session.session_id, s.created.access_token
    request = ask(RETURN_POLICY, client_message_id="4c981eb1-7847-41c0-bf6b-0a0e1b9d6d91")

    first = s.chat.chat(sid, token, request, now=NOW)
    again = s.chat.chat(sid, token, request, now=NOW)

    assert again.replayed and again.response.message_id == first.response.message_id
    assert s.stored()[0].idempotency_key == "4c981eb1-7847-41c0-bf6b-0a0e1b9d6d91"


# --- 9. Response --------------------------------------------------------------------------


def test_the_request_id_is_returned(database: Database) -> None:
    s = build(database)

    turn = s.send(RETURN_POLICY, request_id="req-abc")
    replay_source = s.send(RETURN_POLICY, idempotency_key="k", request_id="req-1")
    replay = s.send(RETURN_POLICY, idempotency_key="k", request_id="req-2")

    assert turn.response.request_id == "req-abc"
    assert replay_source.response.request_id == "req-1"
    assert replay.response.request_id == "req-2"  # the retry's own request


# --- The route only translates HTTP -------------------------------------------------------


@pytest.fixture
def api(make_settings: Callable[..., Settings], faq_service: FAQService) -> Iterator[TestClient]:
    with TestClient(build_app(make_settings, faq_service)) as client:
        yield client


def test_the_route_hands_the_validated_request_to_the_service(api: TestClient) -> None:
    sid, headers = create(api)
    calls: list[dict[str, Any]] = []
    real = api.app.state.customer_chat_service  # type: ignore[attr-defined]

    def recording_chat(*args: Any, **kwargs: Any) -> Any:
        calls.append({"args": args, "kwargs": kwargs})
        return real.chat(*args, **kwargs)

    fake = SimpleNamespace(chat=recording_chat)
    api.app.dependency_overrides[get_customer_chat_service] = lambda: fake  # type: ignore[attr-defined]

    response = api.post(
        f"{BASE}/{sid}/chat",
        json={"message": f"  {RETURN_POLICY}  "},
        headers={**headers, "Idempotency-Key": "req-7"},
    )

    assert response.status_code == 200
    (call,) = calls
    session_id, token, request = call["args"]
    assert session_id == sid and token == headers["Authorization"].removeprefix("Bearer ")
    assert isinstance(request, CustomerChatRequest) and request.message == RETURN_POLICY
    assert call["kwargs"] == {
        "idempotency_key": "req-7",
        "request_id": response.headers["X-Request-ID"],
    }


def test_the_app_uses_the_configured_limit(
    make_settings: Callable[..., Settings], faq_service: FAQService
) -> None:
    app = build_app(make_settings, faq_service, max_customer_message_characters=1234)

    service = app.state.customer_chat_service

    assert isinstance(service, CustomerChatService)
    assert service.max_message_chars == 1234
