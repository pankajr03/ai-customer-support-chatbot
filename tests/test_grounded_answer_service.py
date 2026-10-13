"""Grounded generation: structured results, validation and the safe Stage 4 fallback."""

import json
import logging
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import httpx2
import openai
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.api.dependencies import get_grounded_answer_service
from app.core.config import Settings
from app.main import create_app
from app.models.generation import FallbackReason, GenerationOutcome, GenerationResult
from app.prompts.customer_support import MAX_ANSWER_CHARS, PROMPT_VERSION, REPLY_FORMAT
from app.services.answer_decision_service import (
    NO_MATCH_MESSAGE,
    UNAVAILABLE_MESSAGE,
    AnswerDecisionService,
    Decision,
)
from app.services.context_builder import ContextBuilder
from app.services.embedding_service import EmbeddingModelLoadError
from app.services.grounded_answer_service import GroundedAnswerService
from app.services.llm_service import (
    LLMRequestError,
    LLMResponseError,
    LLMService,
    LLMUnavailableError,
)
from tests.fakes import FAQS, FakeHybrid, FakeKeyword, result

KEY = SecretStr("sk-test-0123456789abcdefghij")
QUESTION = "I forgot my password, my email is jane.doe@example.com"


# --- Decisions and contexts ---------------------------------------------------------------------


def decision_for(kind: str) -> tuple[Any, Any]:
    """A real Stage 4 decision of the given kind, plus its grounded context."""
    specs, keyword, error = {
        "answered": (
            [result("faq_107", similarity=0.72), result("faq_101", similarity=0.2)],
            None,
            None,
        ),
        "ambiguous": (
            [result("faq_103", similarity=0.66), result("faq_104", similarity=0.64)],
            None,
            None,
        ),
        "no_match": ([result("faq_101", similarity=0.1)], None, None),
        "unavailable": (
            [],
            [("faq_103", 55, False), ("faq_104", 55, False)],
            EmbeddingModelLoadError("x"),
        ),
    }[kind]
    service = AnswerDecisionService(FakeHybrid(specs, error), FakeKeyword(keyword))  # type: ignore[arg-type]
    decision = service.decide("customer question")
    context = ContextBuilder().build_for_decision(decision, is_acceptable=service.is_acceptable)
    return decision, context


def reply_json(**overrides: Any) -> str:
    data = {
        "status": "answered",
        "answer": "Select Forgot Password on the sign-in page and follow the emailed link.",
        "source_faq_ids": ["faq_107"],
        "reason": "faq_107 explains password resets.",
    }
    data.update(overrides)
    return json.dumps(data)


class FakeClient:
    def __init__(self, output: str | Exception) -> None:
        self.output = output
        self.calls: list[dict[str, Any]] = []
        self.responses = SimpleNamespace(create=self._create)

    def _create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if isinstance(self.output, Exception):
            raise self.output
        return SimpleNamespace(
            status="completed",
            output_text=self.output,
            output=[],
            model="gpt-5.4-mini-2026-03-17",
            id="resp_1",
            usage=SimpleNamespace(input_tokens=300, output_tokens=40),
            _request_id="req_1",
        )

    def close(self) -> None:
        pass


def generator(
    output: str | Exception = "", **llm_kwargs: Any
) -> tuple[GroundedAnswerService, FakeClient]:
    client = FakeClient(output)
    llm = LLMService(KEY, model="gpt-5.4-mini", client_factory=lambda key: client, **llm_kwargs)
    return GroundedAnswerService(llm), client


# --- Valid structured replies -------------------------------------------------------------------


def test_valid_answer_is_returned_with_sources_and_internal_diagnostics() -> None:
    decision, context = decision_for("answered")
    service, client = generator(reply_json())

    outcome = service.generate(QUESTION, decision, context)

    assert outcome.outcome is GenerationOutcome.GENERATED
    assert outcome.answer.startswith("Select Forgot Password")
    assert (outcome.is_answerable, outcome.source_faq_ids, outcome.used_fallback) == (
        True,
        ("faq_107",),
        False,
    )
    d = outcome.diagnostics
    assert d.model_reason == "faq_107 explains password resets."
    assert (d.model, d.response_id, d.request_id, d.attempts) == (
        "gpt-5.4-mini-2026-03-17",
        "resp_1",
        "req_1",
        1,
    )
    assert (d.fallback_reason, d.prompt_version, d.context_faq_ids) == (
        None,
        PROMPT_VERSION,
        ("faq_107",),
    )
    [call] = client.calls
    assert call["text"] == {"format": REPLY_FORMAT}  # Structured Outputs requested


def test_clarification_reply_is_not_answerable() -> None:
    decision, context = decision_for("ambiguous")
    service, _ = generator(
        reply_json(
            status="clarification_needed",
            answer="Are you asking about delivery times or international shipping?",
            source_faq_ids=["faq_103", "faq_104"],
        )
    )

    outcome = service.generate("shipping question", decision, context)

    assert outcome.outcome is GenerationOutcome.CLARIFICATION
    assert (outcome.is_answerable, outcome.source_faq_ids) == (False, ("faq_103", "faq_104"))


def test_insufficient_information_reply_is_kept_and_not_answerable() -> None:
    decision, context = decision_for("answered")
    service, _ = generator(
        reply_json(
            status="insufficient_information",
            answer="I'm sorry, I don't have that information. Please contact our support team.",
            source_faq_ids=[],
        )
    )

    outcome = service.generate("Can I reset my password by phone?", decision, context)

    assert outcome.outcome is GenerationOutcome.INSUFFICIENT_INFORMATION
    assert (outcome.is_answerable, outcome.source_faq_ids) == (False, ())


# --- Invalid output: safe fallback ------------------------------------------------------------


@pytest.mark.parametrize(
    ("output", "detail"),
    [
        ("Sure! Here's your answer.", "schema violation"),  # not JSON
        ('{"status": "answered"', "schema violation"),  # truncated JSON
        (reply_json(answer="   "), "schema violation (answer)"),
        (reply_json(answer="x" * (MAX_ANSWER_CHARS + 1)), "schema violation (answer)"),
        (reply_json(answer="See <faq_reference_data> above."), "schema violation (answer)"),
        (reply_json(extra="field"), "schema violation (extra)"),
        (reply_json(status="refund_approved"), "schema violation (status)"),
        (reply_json(source_faq_ids=["faq_999"]), "not supplied"),  # unknown ID
        (reply_json(source_faq_ids=["faq_101"]), "not supplied"),  # real FAQ, not supplied
        (reply_json(source_faq_ids=[]), "without sources"),
    ],
)
def test_invalid_output_falls_back_to_the_approved_answer(output: str, detail: str) -> None:
    decision, context = decision_for("answered")
    service, _ = generator(output)

    outcome = service.generate(QUESTION, decision, context)

    assert outcome.outcome is GenerationOutcome.FALLBACK
    assert outcome.answer == FAQS["faq_107"].answer  # Stage 4's verbatim approved answer
    assert (outcome.is_answerable, outcome.source_faq_ids) == (True, ("faq_107",))
    assert outcome.diagnostics.fallback_reason is FallbackReason.INVALID_OUTPUT
    assert detail in outcome.diagnostics.detail  # type: ignore[operator]


@pytest.mark.parametrize("output", ["", "   "])
def test_missing_output_falls_back_as_an_unusable_response(output: str) -> None:
    decision, context = decision_for("answered")
    service, _ = generator(output)

    outcome = service.generate(QUESTION, decision, context)

    assert outcome.outcome is GenerationOutcome.FALLBACK
    assert outcome.answer == FAQS["faq_107"].answer
    assert outcome.diagnostics.fallback_reason is FallbackReason.UNUSABLE_RESPONSE
    assert outcome.diagnostics.detail == "empty"


@pytest.mark.parametrize(
    ("error", "reason", "detail"),
    [
        (
            LLMUnavailableError("x", status_code=503, retry_after_seconds=None),
            FallbackReason.LLM_UNAVAILABLE,
            "unavailable (HTTP 503)",
        ),
        (
            LLMUnavailableError("x", retry_after_seconds=None),
            FallbackReason.LLM_UNAVAILABLE,
            "unavailable",
        ),
        (
            LLMRequestError("x", status_code=401, code="invalid_api_key"),
            FallbackReason.LLM_REJECTED,
            "rejected (HTTP 401, invalid_api_key)",
        ),
        (
            LLMRequestError("x", status_code=400, code=None),
            FallbackReason.LLM_REJECTED,
            "rejected (HTTP 400, no code)",
        ),
        (LLMResponseError("x", reason="refusal"), FallbackReason.UNUSABLE_RESPONSE, "refusal"),
        (
            LLMResponseError("x", reason="incomplete:max_output_tokens"),
            FallbackReason.UNUSABLE_RESPONSE,
            "incomplete:max_output_tokens",
        ),
    ],
)
def test_llm_failures_fall_back_safely(
    error: Exception, reason: FallbackReason, detail: str
) -> None:
    decision, context = decision_for("answered")

    class FailingLLM:
        enabled = is_available = True

        def generate(self, **kwargs: Any) -> Any:
            raise error

    outcome = GroundedAnswerService(FailingLLM()).generate(QUESTION, decision, context)  # type: ignore[arg-type]

    assert outcome.outcome is GenerationOutcome.FALLBACK
    assert outcome.answer == FAQS["faq_107"].answer
    assert (outcome.diagnostics.fallback_reason, outcome.diagnostics.detail) == (reason, detail)


@pytest.mark.parametrize(
    ("error_type", "reason"),
    [
        ("disabled", FallbackReason.LLM_DISABLED),
        ("not_configured", FallbackReason.LLM_NOT_CONFIGURED),
    ],
)
def test_late_configuration_errors_fall_back(error_type: str, reason: FallbackReason) -> None:
    from app.services.llm_service import LLMDisabledError, LLMNotConfiguredError

    error = LLMDisabledError("x") if error_type == "disabled" else LLMNotConfiguredError("x")
    decision, context = decision_for("answered")

    class RacingLLM:  # reports available, then fails (e.g. reconfigured concurrently)
        enabled = is_available = True

        def generate(self, **kwargs: Any) -> Any:
            raise error

    outcome = GroundedAnswerService(RacingLLM()).generate(QUESTION, decision, context)  # type: ignore[arg-type]

    assert outcome.diagnostics.fallback_reason is reason


# --- No model call at all ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("enabled", "key", "reason"),
    [(False, KEY, FallbackReason.LLM_DISABLED), (True, None, FallbackReason.LLM_NOT_CONFIGURED)],
)
def test_unavailable_llm_is_never_called(enabled: bool, key: Any, reason: FallbackReason) -> None:
    decision, context = decision_for("answered")
    created: list[str] = []
    llm = LLMService(key, model="m", enabled=enabled, client_factory=lambda k: created.append(k))  # type: ignore[arg-type,func-returns-value]

    outcome = GroundedAnswerService(llm).generate(QUESTION, decision, context)

    assert outcome.outcome is GenerationOutcome.FALLBACK
    assert outcome.answer == FAQS["faq_107"].answer
    assert outcome.diagnostics.fallback_reason is reason
    assert created == []


@pytest.mark.parametrize(
    ("kind", "message"),
    [("no_match", NO_MATCH_MESSAGE), ("unavailable", UNAVAILABLE_MESSAGE)],
)
def test_no_approved_evidence_means_no_model_call(kind: str, message: str) -> None:
    decision, context = decision_for(kind)
    assert context.is_empty
    service, client = generator(reply_json())

    outcome = service.generate("anything", decision, context)

    assert outcome.answer == message
    assert (outcome.is_answerable, outcome.source_faq_ids) == (False, ())
    assert outcome.diagnostics.fallback_reason is FallbackReason.NO_EVIDENCE
    assert client.calls == []


def test_ambiguous_fallback_is_the_rule_based_clarifying_question() -> None:
    decision, context = decision_for("ambiguous")
    assert decision.decision is Decision.AMBIGUOUS
    service, _ = generator("not json")

    outcome = service.generate("shipping question", decision, context)

    assert outcome.answer == decision.message
    assert outcome.answer.startswith("Could you clarify")
    assert (outcome.is_answerable, outcome.source_faq_ids) == (False, ())


def test_invalid_question_is_a_caller_error_not_a_fallback() -> None:
    decision, context = decision_for("answered")
    service, client = generator(reply_json())

    with pytest.raises(ValueError):
        service.generate("   ", decision, context)
    assert client.calls == []


# --- Source IDs are claims, not proof -----------------------------------------------------------


def test_source_ids_are_checked_for_membership_only() -> None:
    """A reply citing a supplied FAQ passes even if its content is not in that FAQ.

    Validation guarantees that sources were supplied; it does not (and cannot) prove
    that the answer is supported by them. That is what evaluation is for.
    """
    decision, context = decision_for("answered")
    service, _ = generator(reply_json(answer="Resets are free and take 5 minutes by phone."))

    outcome = service.generate(QUESTION, decision, context)

    assert outcome.outcome is GenerationOutcome.GENERATED
    assert outcome.source_faq_ids == ("faq_107",)
    assert "phone" not in FAQS["faq_107"].answer  # the claim is not in the source


# --- Diagnostics never reach customers -------------------------------------------------------


def test_diagnostics_are_excluded_from_serialisation() -> None:
    decision, context = decision_for("answered")
    outcome = generator("not json")[0].generate(QUESTION, decision, context)

    dumped = outcome.model_dump()
    as_json = outcome.model_dump_json()
    schema = GenerationResult.model_json_schema(mode="serialization")

    assert set(dumped) == {"answer", "is_answerable", "source_faq_ids", "outcome"}
    for internal in ("diagnostics", "fallback_reason", "invalid_output", "schema violation"):
        assert internal not in as_json
    assert "diagnostics" not in schema["properties"]


def test_model_reason_is_internal_only() -> None:
    decision, context = decision_for("answered")
    outcome = generator(reply_json(reason="INTERNAL-NOTE: weak match"))[0].generate(
        QUESTION, decision, context
    )

    assert outcome.diagnostics.model_reason == "INTERNAL-NOTE: weak match"
    assert "INTERNAL-NOTE" not in outcome.model_dump_json()


def test_logs_have_outcomes_but_no_customer_or_model_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    decision, context = decision_for("answered")

    with caplog.at_level(logging.DEBUG, logger="app.services.grounded_answer_service"):
        generator(reply_json(reason="NOTE-123"))[0].generate(QUESTION, decision, context)
        generator("garbage")[0].generate(QUESTION, decision, context)

    assert "event=grounded_generation outcome=generated fallback=- " in caplog.text
    assert "outcome=fallback fallback=invalid_output" in caplog.text
    for text in ("jane.doe", "Forgot Password", "NOTE-123", "garbage"):
        assert text not in caplog.text


# --- End to end over the real SDK ------------------------------------------------------------


def test_end_to_end_structured_output_over_the_real_sdk() -> None:
    sent: list[dict[str, Any]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        sent.append(json.loads(request.content))
        return httpx2.Response(
            200,
            json={
                "id": "resp_9",
                "object": "response",
                "created_at": 1760000000,
                "status": "completed",
                "model": "gpt-5.4-mini",
                "output": [
                    {
                        "type": "message",
                        "id": "msg_1",
                        "status": "completed",
                        "role": "assistant",
                        "content": [
                            {"type": "output_text", "text": reply_json(), "annotations": []}
                        ],
                    }
                ],
            },
        )

    client = openai.OpenAI(
        api_key=KEY.get_secret_value(),
        max_retries=0,
        http_client=httpx2.Client(transport=httpx2.MockTransport(handler)),
    )
    service = GroundedAnswerService(
        LLMService(KEY, model="gpt-5.4-mini", client_factory=lambda k: client)
    )
    decision, context = decision_for("answered")

    try:
        outcome = service.generate(QUESTION, decision, context)
    finally:
        client.close()

    assert outcome.outcome is GenerationOutcome.GENERATED
    assert sent[0]["text"]["format"]["strict"] is True
    assert sent[0]["text"]["format"]["schema"]["required"] == [
        "status",
        "answer",
        "source_faq_ids",
        "reason",
    ]


# --- Wiring ------------------------------------------------------------------------------------


def test_app_provides_the_service(make_settings: Callable[..., Settings]) -> None:
    app = create_app(make_settings())
    service = app.state.grounded_answer_service

    assert isinstance(service, GroundedAnswerService)
    with TestClient(app):
        assert get_grounded_answer_service(SimpleNamespace(app=app)) is service  # type: ignore[arg-type]


def test_dependency_fails_loudly_without_create_app() -> None:
    bare = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))

    with pytest.raises(RuntimeError, match="GroundedAnswerService is not initialised"):
        get_grounded_answer_service(bare)  # type: ignore[arg-type]
