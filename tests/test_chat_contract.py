"""The customer chat API contract (Stage 7): models, decision mapping and the endpoint."""

import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api.dependencies import get_customer_chat_service
from app.core.config import Settings
from app.database.connection import Database
from app.models.answer import GenerationMethod, UsageSummary
from app.models.chat import (
    ChatDecision,
    ChatSourceReference,
    ChatUsageMetadata,
    CustomerChatRequest,
    CustomerChatResponse,
    chat_decision,
    decision_for_outcome,
    source_references,
)
from app.models.conversation import (
    MAX_USER_MESSAGE_LENGTH,
    ConversationMessage,
    ConversationSession,
)
from app.models.errors import ErrorResponse
from app.models.generation import GenerationOutcome, GenerationResult
from app.services.answer_decision_service import (
    NO_MATCH_MESSAGE,
    AnswerDecision,
    Decision,
    RetrievalMode,
)
from app.services.context_builder import GroundedContext
from app.services.customer_chat_service import (
    ClientMessageIdMismatchError,
    KnowledgeBaseUnavailableError,
    resolve_idempotency_key,
)
from app.services.faq_service import FAQService
from app.services.grounded_answer_service import GroundedAnswer
from tests.fakes import FAQS
from tests.test_conversations_api import BASE, QUESTION, build_app, create
from tests.test_customer_chat_service import build

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
CLIENT_ID = uuid.UUID("4c981eb1-7847-41c0-bf6b-0a0e1b9d6d91")
RESPONSE_FIELDS = {
    "session_id",
    "message_id",
    "client_message_id",
    "request_id",
    "decision",
    "answer",
    "generation_method",
    "sources",
    "usage",
    "created_at",
    "user_message",  # deprecated (Stage 6 shape)
    "assistant_message",  # deprecated (Stage 6 shape)
}


@pytest.fixture
def database() -> Iterator[Database]:
    db = Database("sqlite://")
    db.init_schema()
    yield db
    db.dispose()


def exchange(
    decision: Decision = Decision.ANSWERED,
    sources: tuple[str, ...] = ("faq_106",),
    key: str | None = None,
) -> tuple[ConversationMessage, ConversationMessage]:
    session = ConversationSession.new(ttl=timedelta(minutes=30), now=NOW)
    user = ConversationMessage.user_message(
        session.session_id, 1, "Can I return my order?", now=NOW, idempotency_key=key
    )
    reply = ConversationMessage.assistant_message(
        session.session_id, 2, "Reply text.", decision, sources, now=NOW + timedelta(seconds=1)
    )
    return user, reply


def refs(*faq_ids: str) -> list[ChatSourceReference]:
    return [ChatSourceReference(faq_id=i, question=FAQS[i].question) for i in faq_ids]


# --- Decisions ------------------------------------------------------------------------


def test_public_decisions_are_exactly_the_four_supported_values() -> None:
    assert {d.value for d in ChatDecision} == {
        "answered",
        "no_match",
        "ambiguous",
        "retrieval_unavailable",
    }


@pytest.mark.parametrize("decision", list(Decision))
def test_every_internal_decision_is_mapped(decision: Decision) -> None:
    assert chat_decision(decision).value == decision.value


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        (GenerationOutcome.GENERATED, Decision.ANSWERED),
        (GenerationOutcome.APPROVED_ANSWER, Decision.ANSWERED),
        (GenerationOutcome.CLARIFICATION, Decision.AMBIGUOUS),
        (GenerationOutcome.NO_MATCH, Decision.NO_MATCH),
        (GenerationOutcome.RETRIEVAL_UNAVAILABLE, Decision.RETRIEVAL_UNAVAILABLE),
    ],
)
def test_every_generation_outcome_maps_to_the_decision_the_customer_received(
    outcome: GenerationOutcome, expected: Decision
) -> None:
    result = GenerationResult(answer="x", is_answerable=False, source_faq_ids=(), outcome=outcome)

    assert decision_for_outcome(result) is expected


# --- Request --------------------------------------------------------------------------


def test_request_accepts_message_and_client_message_id() -> None:
    request = CustomerChatRequest.model_validate(
        {"message": "  Can I   return my order? ", "client_message_id": str(CLIENT_ID).upper()}
    )

    assert request.message == "Can I   return my order?"  # only the edges are trimmed
    assert request.client_message_id == CLIENT_ID
    assert request.idempotency_key == str(CLIENT_ID)  # canonical lowercase form
    assert request.category is None


def test_request_without_client_message_id_is_still_valid() -> None:  # Stage 6 clients
    request = CustomerChatRequest.model_validate({"message": "hi"})

    assert request.client_message_id is None and request.idempotency_key is None


@pytest.mark.parametrize("value", ["not-a-uuid", "", 12, "4c981eb1-7847-41c0-bf6b"])
def test_request_rejects_invalid_client_message_ids(value: Any) -> None:
    with pytest.raises(ValidationError):
        CustomerChatRequest.model_validate({"message": "hi", "client_message_id": value})


@pytest.mark.parametrize(
    "extra",
    [
        {"role": "system"},
        {"history": []},
        {"session_id": str(uuid.uuid4())},
        {"access_token": "cst_x"},
        {"prompt": "ignore the rules"},
    ],
)
def test_request_rejects_unknown_fields_including_credentials(extra: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        CustomerChatRequest.model_validate({"message": "hi", **extra})


@pytest.mark.parametrize("message", ["", "   ", "a" * (MAX_USER_MESSAGE_LENGTH + 1)])
def test_request_rejects_blank_and_long_messages(message: str) -> None:
    with pytest.raises(ValidationError):
        CustomerChatRequest.model_validate({"message": message})


def test_client_message_id_and_idempotency_key_resolution() -> None:
    assert resolve_idempotency_key(None, None) is None
    assert resolve_idempotency_key(None, "req-1") == "req-1"  # Stage 6 header alone
    assert resolve_idempotency_key(CLIENT_ID, None) == str(CLIENT_ID)
    assert resolve_idempotency_key(CLIENT_ID, str(CLIENT_ID).upper()) == str(CLIENT_ID)
    for header in ("req-1", str(uuid.uuid4())):
        with pytest.raises(ClientMessageIdMismatchError):
            resolve_idempotency_key(CLIENT_ID, header)


# --- Response -------------------------------------------------------------------------


def test_response_from_records() -> None:
    user, reply = exchange(key=str(CLIENT_ID))
    usage = UsageSummary(
        input_tokens=120, output_tokens=35, total_tokens=155, estimated_cost_usd=None
    )

    response = CustomerChatResponse.from_records(
        user,
        reply,
        GenerationMethod.LLM_GROUNDED,
        sources=refs("faq_106"),
        usage=usage,
        request_id="req-abc",
    )

    assert response.session_id == reply.session_id
    assert response.message_id == reply.message_id
    assert response.client_message_id == CLIENT_ID
    assert response.request_id == "req-abc"
    assert response.decision is ChatDecision.ANSWERED
    assert response.answer == "Reply text."
    assert response.sources == refs("faq_106")
    assert response.usage == ChatUsageMetadata(**usage.model_dump())
    assert response.created_at == reply.created_at
    assert response.created_at.utcoffset() == timedelta(0)
    assert set(response.model_dump()) == RESPONSE_FIELDS


def test_non_uuid_idempotency_keys_give_no_client_message_id() -> None:
    user, reply = exchange(key="req-1")

    response = CustomerChatResponse.from_records(
        user, reply, GenerationMethod.APPROVED_FAQ, sources=refs("faq_106")
    )

    assert response.client_message_id is None


def test_absent_usage_is_distinct_from_zero_usage() -> None:
    user, reply = exchange()
    zero = UsageSummary(input_tokens=0, output_tokens=0, total_tokens=0, estimated_cost_usd=0.0)
    unreported = UsageSummary(
        input_tokens=None, output_tokens=None, total_tokens=None, estimated_cost_usd=None
    )

    def build_with(usage: UsageSummary | None) -> Any:
        return CustomerChatResponse.from_records(
            user, reply, GenerationMethod.APPROVED_FAQ, sources=refs("faq_106"), usage=usage
        ).model_dump()["usage"]

    assert build_with(None) is None  # no LLM response at all
    assert build_with(zero) == {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "estimated_cost_usd": 0.0,
    }
    assert build_with(unreported) == dict.fromkeys(
        ("input_tokens", "output_tokens", "total_tokens", "estimated_cost_usd")
    )
    with pytest.raises(ValidationError):
        ChatUsageMetadata(input_tokens=-1, output_tokens=0, total_tokens=0, estimated_cost_usd=None)


@pytest.mark.parametrize(
    ("decision", "method", "sources"),
    [
        (Decision.ANSWERED, GenerationMethod.LLM_GROUNDED, ("faq_106",)),
        (Decision.ANSWERED, GenerationMethod.APPROVED_FAQ, ("faq_106",)),
        (Decision.AMBIGUOUS, GenerationMethod.CLARIFICATION, ("faq_103", "faq_104")),
        (Decision.NO_MATCH, GenerationMethod.FALLBACK, ()),
        (Decision.RETRIEVAL_UNAVAILABLE, GenerationMethod.FALLBACK, ()),
    ],
)
def test_consistent_decisions_are_accepted(
    decision: Decision, method: GenerationMethod, sources: tuple[str, ...]
) -> None:
    user, reply = exchange(decision, sources)

    response = CustomerChatResponse.from_records(user, reply, method, sources=refs(*sources))

    assert response.decision.value == decision.value
    assert [s.faq_id for s in response.sources] == list(sources)


@pytest.mark.parametrize(
    ("decision", "method"),
    [
        # An infrastructure failure or a fallback must never look like an answer ...
        (Decision.RETRIEVAL_UNAVAILABLE, GenerationMethod.LLM_GROUNDED),
        (Decision.RETRIEVAL_UNAVAILABLE, GenerationMethod.APPROVED_FAQ),
        (Decision.NO_MATCH, GenerationMethod.APPROVED_FAQ),
        (Decision.AMBIGUOUS, GenerationMethod.LLM_GROUNDED),
        # ... and an answer must not be labelled as a fallback.
        (Decision.ANSWERED, GenerationMethod.FALLBACK),
        (Decision.ANSWERED, GenerationMethod.CLARIFICATION),
    ],
)
def test_inconsistent_decision_and_method_are_rejected(
    decision: Decision, method: GenerationMethod
) -> None:
    user, reply = exchange(decision, ("faq_106",) if decision is Decision.ANSWERED else ())

    with pytest.raises(ValidationError, match="not valid for"):
        CustomerChatResponse.from_records(user, reply, method)


@pytest.mark.parametrize("decision", [Decision.NO_MATCH, Decision.RETRIEVAL_UNAVAILABLE])
def test_fallbacks_cannot_have_sources(decision: Decision) -> None:
    user, reply = exchange(decision, ("faq_106",))

    with pytest.raises(ValidationError, match="cannot have sources"):
        CustomerChatResponse.from_records(
            user, reply, GenerationMethod.FALLBACK, sources=refs("faq_106")
        )


def test_answers_need_a_source_faq() -> None:
    user, reply = exchange(Decision.ANSWERED, ())

    with pytest.raises(ValidationError, match="at least one FAQ"):
        CustomerChatResponse.from_records(user, reply, GenerationMethod.APPROVED_FAQ)


def test_sources_must_come_from_the_stored_evidence() -> None:
    user, reply = exchange(Decision.ANSWERED, ("faq_106",))

    with pytest.raises(ValueError, match="stored reply's source FAQs"):
        CustomerChatResponse.from_records(
            user, reply, GenerationMethod.APPROVED_FAQ, sources=refs("faq_106", "faq_107")
        )


def test_sources_follow_the_stored_order() -> None:
    user, reply = exchange(Decision.AMBIGUOUS, ("faq_104", "faq_103"))

    response = CustomerChatResponse.from_records(
        user, reply, GenerationMethod.CLARIFICATION, sources=refs("faq_103", "faq_104")
    )

    assert [s.faq_id for s in response.sources] == ["faq_104", "faq_103"]


def test_source_references_skip_unknown_ids_and_never_invent_questions() -> None:
    questions = {"faq_106": FAQS["faq_106"].question}

    assert source_references(["faq_999", "faq_106"], questions) == refs("faq_106")


def test_source_reference_validation() -> None:
    with pytest.raises(ValidationError):
        ChatSourceReference(faq_id="not-an-id", question="Q?")
    with pytest.raises(ValidationError):
        ChatSourceReference(faq_id="faq_001", question="")
    with pytest.raises(ValidationError):
        ChatSourceReference.model_validate({"faq_id": "faq_001", "question": "Q?", "answer": "A"})


def test_response_rejects_extra_fields_and_naive_timestamps() -> None:
    user, reply = exchange()
    data = CustomerChatResponse.from_records(
        user, reply, GenerationMethod.APPROVED_FAQ, sources=refs("faq_106")
    ).model_dump()

    for extra in ({"prompt": "system"}, {"diagnostics": {}}, {"rewritten_query": "q"}):
        with pytest.raises(ValidationError):
            CustomerChatResponse.model_validate({**data, **extra})
    with pytest.raises(ValidationError):
        CustomerChatResponse.model_validate({**data, "created_at": datetime(2026, 10, 15, 12)})


def test_error_response_rejects_internal_fields() -> None:
    body = {"error": {"code": "SESSION_NOT_FOUND", "message": "m", "request_id": None}}

    assert ErrorResponse.model_validate(body).error.code == "SESSION_NOT_FOUND"
    with pytest.raises(ValidationError):
        ErrorResponse.model_validate({"error": {**body["error"], "exception": "Traceback"}})


# --- Service: decisions describe what the customer received ---------------------------


class FakeGrounded:
    """Returns a fixed grounded answer, to simulate pipeline edge cases."""

    def __init__(self, answer: GroundedAnswer) -> None:
        self._answer = answer

    def answer(self, question: str, **kwargs: Any) -> GroundedAnswer:
        return self._answer


def grounded(decision: Decision, result: GenerationResult, faq_id: str | None) -> GroundedAnswer:
    stage4 = AnswerDecision(
        decision=decision,
        message="m",
        retrieval_mode=RetrievalMode.HYBRID,
        reasons=(),
        faq=FAQS[faq_id] if faq_id else None,
    )
    context = GroundedContext(records=(), omitted=(), text="", max_records=3, max_chars=6000)
    return GroundedAnswer(result=result, decision=stage4, context=context)


def test_stage4_answer_that_fell_back_to_no_match_is_reported_as_no_match(
    database: Database,
) -> None:
    fallback = GenerationResult(
        answer=NO_MATCH_MESSAGE,
        is_answerable=False,
        source_faq_ids=(),
        outcome=GenerationOutcome.NO_MATCH,
    )
    s = build(database, grounded=FakeGrounded(grounded(Decision.ANSWERED, fallback, None)))

    response = s.send("What is your return policy?").response

    assert response.decision is ChatDecision.NO_MATCH
    assert response.generation_method is GenerationMethod.FALLBACK
    assert response.sources == []
    assert s.stored()[1].decision is Decision.NO_MATCH  # history agrees with the customer


def test_source_ids_outside_the_evidence_are_never_shown(database: Database) -> None:
    claimed = GenerationResult(
        answer=FAQS["faq_106"].answer,
        is_answerable=True,
        source_faq_ids=("faq_106", "faq_999"),
        outcome=GenerationOutcome.APPROVED_ANSWER,
    )
    s = build(database, grounded=FakeGrounded(grounded(Decision.ANSWERED, claimed, "faq_106")))

    response = s.send("What is your return policy?").response

    assert response.sources == refs("faq_106")
    assert s.stored()[1].source_faq_ids == ("faq_106",)


@pytest.mark.parametrize(
    ("message", "decision", "method", "sources"),
    [
        ("What is your return policy?", "answered", "approved_faq", ["faq_106"]),
        ("Tell me about delivery", "ambiguous", "clarification", ["faq_103", "faq_104"]),
        ("Do you sell submarines?", "no_match", "fallback", []),
    ],
)
def test_service_turns_follow_the_contract(
    database: Database, message: str, decision: str, method: str, sources: list[str]
) -> None:
    s = build(database)

    response = s.send(message).response

    assert response.decision == decision
    assert response.generation_method == method
    assert [ref.faq_id for ref in response.sources] == sources
    assert all(ref.question == FAQS[ref.faq_id].question for ref in response.sources)
    assert response.answer == response.assistant_message.content


def test_replay_lists_sources_from_the_approved_knowledge_base(database: Database) -> None:
    s = build(database, faq_lookup=FAQS.get)
    first = s.send("What is your return policy?", idempotency_key=str(CLIENT_ID)).response

    again = s.send("What is your return policy?", idempotency_key=str(CLIENT_ID)).response

    assert again.message_id == first.message_id
    assert again.client_message_id == CLIENT_ID
    assert again.sources == first.sources == refs("faq_106")
    assert again.usage is None


def test_replay_leaves_out_withdrawn_faqs(database: Database) -> None:
    s = build(database, faq_lookup=lambda faq_id: None)  # every FAQ withdrawn since
    s.send("What is your return policy?", idempotency_key="req-1")

    again = s.send("What is your return policy?", idempotency_key="req-1").response

    assert again.decision is ChatDecision.ANSWERED
    assert again.sources == []  # never an invented question
    assert again.assistant_message.source_faq_ids == ["faq_106"]


# --- HTTP -----------------------------------------------------------------------------


@pytest.fixture
def api(make_settings: Callable[..., Settings], faq_service: FAQService) -> Iterator[TestClient]:
    with TestClient(build_app(make_settings, faq_service)) as client:
        yield client


def chat(api: TestClient, sid: str, headers: dict[str, str], **body: Any) -> Any:
    return api.post(f"{BASE}/{sid}/chat", json=body, headers=headers)


def test_http_answered_response(api: TestClient) -> None:
    sid, headers = create(api)

    response = chat(api, sid, headers, message=QUESTION, client_message_id=str(CLIENT_ID))

    assert response.status_code == 200
    body = response.json()
    assert set(body) == RESPONSE_FIELDS
    assert body["session_id"] == sid
    assert body["message_id"] == body["assistant_message"]["message_id"]
    assert body["client_message_id"] == str(CLIENT_ID)
    assert body["request_id"] == response.headers["X-Request-ID"]
    assert body["decision"] == "answered"
    assert body["answer"] == FAQS["faq_107"].answer
    assert body["generation_method"] == "approved_faq"
    assert body["sources"] == [{"faq_id": "faq_107", "question": FAQS["faq_107"].question}]
    assert body["usage"] is None  # LLM disabled: absent, not zero
    assert datetime.fromisoformat(body["created_at"]).utcoffset() == timedelta(0)


def keys(value: Any) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {k for v in value.values() for k in keys(v)}
    if isinstance(value, list):
        return {k for v in value for k in keys(v)}
    return set()


def test_http_response_exposes_no_internal_details(api: TestClient) -> None:
    sid, headers = create(api)
    chat(api, sid, headers, message="What is your return policy?")

    response = chat(api, sid, headers, message="What about after 30 days?")

    internal = {"prompt", "diagnostics", "rewritten_query", "query", "reason", "model_reason"}
    assert keys(response.json()).isdisjoint(internal | {"access_token", "sequence"})
    assert headers["Authorization"].removeprefix("Bearer ") not in response.text


def test_http_retry_with_client_message_id_replays(api: TestClient) -> None:
    sid, headers = create(api)
    first = chat(api, sid, headers, message=QUESTION, client_message_id=str(CLIENT_ID))

    again = chat(api, sid, headers, message=QUESTION, client_message_id=str(CLIENT_ID))

    assert again.headers["Idempotent-Replayed"] == "true"
    assert again.json()["message_id"] == first.json()["message_id"]
    assert again.json()["sources"] == first.json()["sources"]
    assert again.json()["request_id"] != first.json()["request_id"]  # the retry's own ID
    history = api.get(f"{BASE}/{sid}/messages", headers=headers).json()
    assert history["total_messages"] == 2


def test_http_client_message_id_and_header_must_agree(api: TestClient) -> None:
    sid, headers = create(api)
    same = {**headers, "Idempotency-Key": str(CLIENT_ID).upper()}
    other = {**headers, "Idempotency-Key": str(uuid.uuid4())}

    ok = chat(api, sid, same, message=QUESTION, client_message_id=str(CLIENT_ID))
    assert ok.status_code == 200
    response = chat(api, sid, other, message=QUESTION, client_message_id=str(CLIENT_ID))

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "DUPLICATE_MESSAGE_CONFLICT"
    assert api.get(f"{BASE}/{sid}/messages", headers=headers).json()["total_messages"] == 2


def test_http_reused_client_message_id_for_another_message_is_422(api: TestClient) -> None:
    sid, headers = create(api)
    chat(api, sid, headers, message=QUESTION, client_message_id=str(CLIENT_ID))

    response = chat(api, sid, headers, message="Something else", client_message_id=str(CLIENT_ID))

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "DUPLICATE_MESSAGE_CONFLICT"


@pytest.mark.parametrize(
    "body",
    [
        {"message": QUESTION, "client_message_id": "not-a-uuid"},
        {"message": QUESTION, "access_token": "cst_x"},
        {"message": QUESTION, "session_id": str(uuid.uuid4())},
    ],
)
def test_http_rejects_invalid_ids_and_credentials_in_the_body(
    api: TestClient, body: dict[str, Any]
) -> None:
    sid, headers = create(api)

    assert api.post(f"{BASE}/{sid}/chat", json=body, headers=headers).status_code == 422


def test_http_errors_carry_stable_codes(api: TestClient) -> None:
    sid, headers = create(api)
    token = headers["Authorization"].removeprefix("Bearer ")

    missing = chat(api, sid, {}, message=QUESTION)
    in_url = api.post(f"{BASE}/{sid}/chat?access_token={token}", json={"message": QUESTION})
    unknown = chat(api, str(uuid.uuid4()), headers, message=QUESTION)
    api.post(f"{BASE}/{sid}/close", headers=headers)
    closed = chat(api, sid, headers, message=QUESTION)

    def code(response: Any) -> tuple[int, str]:
        return response.status_code, response.json()["error"]["code"]

    assert code(missing) == (401, "UNAUTHORIZED_SESSION")
    assert missing.headers["WWW-Authenticate"] == "Bearer"
    assert code(in_url) == (400, "VALIDATION_ERROR")
    assert code(unknown) == (404, "SESSION_NOT_FOUND")
    assert code(closed) == (409, "SESSION_CLOSED")
    for response in (missing, in_url, unknown, closed):
        assert response.headers["Cache-Control"] == "no-store"
        assert response.json()["error"]["request_id"] == response.headers["X-Request-ID"]
        ErrorResponse.model_validate(response.json())


def test_http_knowledge_base_failure_is_a_503(api: TestClient) -> None:
    sid, headers = create(api)

    def broken(*args: Any, **kwargs: Any) -> Any:
        raise KnowledgeBaseUnavailableError()

    api.app.dependency_overrides[get_customer_chat_service] = lambda: SimpleNamespace(  # type: ignore[attr-defined]
        chat=broken
    )

    response = chat(api, sid, headers, message=QUESTION)

    assert response.status_code == 503
    body = response.json()
    assert body["error"]["code"] == "SERVICE_UNAVAILABLE"
    assert "private" not in response.text and "FAQFileNotFoundError" not in response.text
    ErrorResponse.model_validate(body)


def test_openapi_documents_the_contract(api: TestClient) -> None:
    schema = api.get("/openapi.json").json()
    components = schema["components"]["schemas"]
    operation = schema["paths"][f"{BASE}/{{session_id}}/chat"]["post"]

    assert set(components["ChatDecision"]["enum"]) == {d.value for d in ChatDecision}
    assert set(components["GenerationMethod"]["enum"]) == {m.value for m in GenerationMethod}
    response_schema = components["CustomerChatResponse"]
    assert response_schema["properties"]["user_message"]["deprecated"] is True
    assert response_schema["properties"]["assistant_message"]["deprecated"] is True
    assert "access_token" not in components["CustomerChatRequest"]["properties"]
    for code in ("401", "404", "409", "410", "422", "503"):
        ref = operation["responses"][code]["content"]["application/json"]["schema"]["$ref"]
        assert ref.endswith("/ErrorResponse")
