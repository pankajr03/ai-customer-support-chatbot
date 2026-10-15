"""OpenAI API failure handling: categories, bounded retries, circuit breaker, fallbacks.

Retries and errors run through the real ``openai.OpenAI`` client over an offline mock
transport, so the SDK's own exception mapping is exercised. A fake clock drives the
circuit breaker. No network is used.
"""

import json
import logging
from collections.abc import Callable, Iterator
from types import SimpleNamespace
from typing import Any

import httpx2
import openai
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from app.api.dependencies import get_grounded_answer_service
from app.core.config import Settings
from app.main import create_app
from app.models.generation import FallbackReason, GenerationOutcome
from app.services.answer_decision_service import AnswerDecisionService
from app.services.context_builder import ContextBuilder
from app.services.embedding_service import EmbeddingService
from app.services.faq_service import FAQService
from app.services.grounded_answer_service import GroundedAnswerService
from app.services.llm_service import (
    CircuitBreaker,
    ErrorCategory,
    LLMRequestError,
    LLMService,
    LLMUnavailableError,
)
from app.services.semantic_search_service import SemanticSearchService
from tests.fakes import FAKE_MODEL_NAME, FAQS, CountingLoader, FakeHybrid, FakeKeyword
from tests.fakes import result as hybrid_spec

KEY = SecretStr("sk-test-SECRET-0123456789abcdef")
OK_BODY = {
    "id": "resp_ok",
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
            "content": [{"type": "output_text", "text": "ok", "annotations": []}],
        }
    ],
}


def error(status: int, code: str | None = None, headers: dict[str, str] | None = None) -> tuple:
    message = f"error with key {KEY.get_secret_value()}"  # the API may echo key fragments
    return (status, {"error": {"message": message, "type": "e", "code": code}}, headers)


OK = (200, OK_BODY, None)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class Transport:
    def __init__(self, outcomes: list[Any]) -> None:
        self.outcomes = list(outcomes)
        self.requests = 0

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests += 1
        outcome = self.outcomes[min(self.requests, len(self.outcomes)) - 1]
        if isinstance(outcome, Exception):
            raise outcome
        status, body, headers = outcome
        return httpx2.Response(status, json=body, headers=headers or {})


@pytest.fixture
def sdk() -> Iterator[Callable[..., tuple[LLMService, Transport, FakeClock]]]:
    clients: list[openai.OpenAI] = []

    def _make(
        outcomes: list[Any],
        *,
        max_retries: int = 0,
        threshold: int = 3,
        cooldown: float = 60.0,
    ) -> tuple[LLMService, Transport, FakeClock]:
        transport, clock = Transport(outcomes), FakeClock()

        def factory(api_key: str) -> openai.OpenAI:
            client = openai.OpenAI(
                api_key=api_key,
                max_retries=0,
                http_client=httpx2.Client(transport=httpx2.MockTransport(transport)),
            )
            clients.append(client)
            return client

        llm = LLMService(
            KEY,
            model="gpt-5.4-mini",
            max_retries=max_retries,
            circuit_breaker=CircuitBreaker(
                failure_threshold=threshold, cooldown_seconds=cooldown, clock=clock
            ),
            client_factory=factory,
            sleep=lambda seconds: None,
        )
        return llm, transport, clock

    yield _make
    for client in clients:
        client.close()


def call(llm: LLMService) -> Any:
    return llm.generate(instructions="Answer from FAQs.", input="question")


# --- Categories and retries -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("outcome", "category", "error_type", "requests"),
    [
        (error(401, "invalid_api_key"), ErrorCategory.AUTHENTICATION, LLMRequestError, 1),
        (error(403), ErrorCategory.PERMISSION, LLMRequestError, 1),
        (error(404, "model_not_found"), ErrorCategory.INVALID_CONFIGURATION, LLMRequestError, 1),
        (
            error(400, "unsupported_parameter"),
            ErrorCategory.INVALID_CONFIGURATION,
            LLMRequestError,
            1,
        ),
        (error(400, "invalid_request_error"), ErrorCategory.INVALID_REQUEST, LLMRequestError, 1),
        (error(422), ErrorCategory.INVALID_REQUEST, LLMRequestError, 1),
        (error(429, "insufficient_quota"), ErrorCategory.QUOTA_EXHAUSTED, LLMRequestError, 1),
        (error(429, "rate_limit_exceeded"), ErrorCategory.RATE_LIMITED, LLMUnavailableError, 3),
        (error(408), ErrorCategory.TIMEOUT, LLMUnavailableError, 3),
        (error(500), ErrorCategory.SERVER_ERROR, LLMUnavailableError, 3),
        (error(503), ErrorCategory.SERVER_ERROR, LLMUnavailableError, 3),
        (httpx2.ReadTimeout("timed out"), ErrorCategory.TIMEOUT, LLMUnavailableError, 3),
        (httpx2.ConnectError("refused"), ErrorCategory.NETWORK, LLMUnavailableError, 3),
    ],
)
def test_each_failure_is_categorised_and_only_transient_ones_are_retried(
    sdk: Callable[..., Any],
    outcome: Any,
    category: ErrorCategory,
    error_type: type[Exception],
    requests: int,
) -> None:
    llm, transport, _ = sdk([outcome], max_retries=2)

    with pytest.raises(error_type) as caught:
        call(llm)

    assert caught.value.category is category  # type: ignore[attr-defined]
    assert transport.requests == requests  # 1 attempt, or 1 + max_retries for transient ones
    assert llm.error_counts() == {category.value: 1}


def test_retries_are_bounded_even_at_the_maximum_setting(sdk: Callable[..., Any]) -> None:
    llm, transport, _ = sdk([error(500)], max_retries=5)

    with pytest.raises(LLMUnavailableError):
        call(llm)

    assert transport.requests == 6  # never more than 1 + 5
    with pytest.raises(ValueError):
        LLMService(KEY, model="m", max_retries=6)


def test_a_transient_failure_then_success_is_one_logical_call(sdk: Callable[..., Any]) -> None:
    llm, transport, _ = sdk([error(503), OK], max_retries=2)

    assert call(llm).attempts == 2
    assert transport.requests == 2 and llm.error_counts() == {}


@pytest.mark.parametrize(
    "raised",
    [KeyError("secret-detail"), openai.OpenAIError("secret-detail"), RuntimeError("secret-detail")],
)
def test_unexpected_client_errors_become_a_categorised_failure(raised: Exception) -> None:
    def create(**kwargs: Any) -> Any:
        raise raised

    client = SimpleNamespace(responses=SimpleNamespace(create=create), close=lambda: None)
    llm = LLMService(KEY, model="m", max_retries=2, client_factory=lambda key: client)

    with pytest.raises(LLMUnavailableError) as caught:
        call(llm)

    assert caught.value.category is ErrorCategory.UNEXPECTED
    assert str(caught.value) == f"Unexpected LLM client error ({type(raised).__name__})"
    assert "secret-detail" not in str(caught.value)
    assert caught.value.__cause__ is None
    assert llm.error_counts() == {"unexpected": 1}


def test_a_malformed_api_body_is_an_unusable_response_not_a_crash(
    sdk: Callable[..., Any],
) -> None:
    llm, transport, _ = sdk([(200, ["not", "an", "object"], None)], max_retries=2)

    with pytest.raises(Exception) as caught:
        call(llm)

    assert caught.value.category is ErrorCategory.UNUSABLE_RESPONSE  # type: ignore[attr-defined]
    assert transport.requests == 1  # a response arrived: not retried


def test_missing_key_is_categorised_without_any_request() -> None:
    created: list[str] = []
    llm = LLMService(None, model="m", client_factory=lambda key: created.append(key))  # type: ignore[arg-type,func-returns-value]

    with pytest.raises(Exception) as caught:
        call(llm)

    assert caught.value.category is ErrorCategory.NOT_CONFIGURED  # type: ignore[attr-defined]
    assert created == [] and llm.error_counts() == {"not_configured": 1}


# --- Circuit breaker ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "outcome",
    [
        error(401, "invalid_api_key"),
        error(403),
        error(404, "model_not_found"),
        error(429, "insufficient_quota"),
    ],
)
def test_configuration_failures_pause_calls_at_once(
    sdk: Callable[..., Any], outcome: tuple
) -> None:
    llm, transport, clock = sdk([outcome])

    with pytest.raises(LLMRequestError):
        call(llm)
    with pytest.raises(LLMUnavailableError) as caught:
        call(llm)

    assert transport.requests == 1  # the second request was never sent
    assert caught.value.category is ErrorCategory.CIRCUIT_OPEN
    assert caught.value.retry_after_seconds == pytest.approx(60)


def test_circuit_allows_a_trial_call_after_the_cooldown(sdk: Callable[..., Any]) -> None:
    llm, transport, clock = sdk([error(401), OK, OK])
    with pytest.raises(LLMRequestError):
        call(llm)

    clock.advance(59)
    with pytest.raises(LLMUnavailableError):
        call(llm)
    clock.advance(2)
    assert call(llm).text == "ok"  # trial call succeeded: circuit closed
    assert call(llm).text == "ok"
    assert transport.requests == 3


def test_failed_trial_call_reopens_the_circuit(sdk: Callable[..., Any]) -> None:
    llm, transport, clock = sdk([error(500)], threshold=3)
    for _ in range(3):
        with pytest.raises(LLMUnavailableError):
            call(llm)
    assert transport.requests == 3

    clock.advance(61)
    with pytest.raises(LLMUnavailableError) as trial:
        call(llm)
    with pytest.raises(LLMUnavailableError) as paused:
        call(llm)

    assert trial.value.category is ErrorCategory.SERVER_ERROR
    assert paused.value.category is ErrorCategory.CIRCUIT_OPEN
    assert transport.requests == 4  # one trial, then paused again


def test_transient_failures_open_the_circuit_only_at_the_threshold(
    sdk: Callable[..., Any],
) -> None:
    llm, transport, _ = sdk([error(503), error(503), OK, error(503), error(503), OK], threshold=3)

    outcomes = []
    for _ in range(6):
        try:
            call(llm)
            outcomes.append("ok")
        except LLMUnavailableError as exc:
            outcomes.append(exc.category.value)

    # A success resets the count, so the circuit never opens.
    assert outcomes == ["server_error", "server_error", "ok", "server_error", "server_error", "ok"]
    assert transport.requests == 6


def test_long_retry_after_pauses_calls_for_that_long(sdk: Callable[..., Any]) -> None:
    llm, transport, clock = sdk([error(429, "rate_limit_exceeded", {"retry-after": "120"}), OK])
    with pytest.raises(LLMUnavailableError):
        call(llm)

    clock.advance(90)
    with pytest.raises(LLMUnavailableError) as caught:
        call(llm)
    clock.advance(31)

    assert caught.value.category is ErrorCategory.CIRCUIT_OPEN
    assert caught.value.retry_after_seconds == pytest.approx(30)
    assert call(llm).text == "ok"
    assert transport.requests == 2


def test_request_specific_errors_do_not_pause_calls(sdk: Callable[..., Any]) -> None:
    llm, transport, _ = sdk([error(400, "invalid_request_error")], threshold=1)

    for _ in range(3):
        with pytest.raises(LLMRequestError):
            call(llm)

    assert transport.requests == 3


def test_an_unusable_reply_is_not_an_outage(sdk: Callable[..., Any]) -> None:
    incomplete = dict(
        OK_BODY, status="incomplete", incomplete_details={"reason": "max_output_tokens"}
    )
    llm, transport, _ = sdk([(200, incomplete, None)], threshold=1)

    for _ in range(3):
        with pytest.raises(Exception) as caught:
            call(llm)
        assert caught.value.category is ErrorCategory.UNUSABLE_RESPONSE  # type: ignore[attr-defined]

    assert transport.requests == 3
    assert llm.error_counts() == {"unusable_response": 3}


def test_cooldown_zero_disables_the_breaker(sdk: Callable[..., Any]) -> None:
    llm, transport, _ = sdk([error(401)], cooldown=0)

    for _ in range(3):
        with pytest.raises(LLMRequestError):
            call(llm)

    assert transport.requests == 3


@pytest.mark.parametrize(
    "kwargs",
    [
        {"failure_threshold": 0},
        {"failure_threshold": 21},
        {"failure_threshold": True},
        {"cooldown_seconds": -1},
        {"cooldown_seconds": 3601},
    ],  # fmt: skip
)
def test_invalid_breaker_settings_are_rejected(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        CircuitBreaker(**kwargs)


def test_breaker_settings(
    make_settings: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = make_settings()
    assert (settings.llm_failure_threshold, settings.llm_failure_cooldown_seconds) == (3, 60.0)

    llm = LLMService.from_settings(
        make_settings(llm_failure_threshold=5, llm_failure_cooldown_seconds=10)
    )
    assert (llm.circuit_breaker.failure_threshold, llm.circuit_breaker.cooldown_seconds) == (5, 10)

    for name, value in (("LLM_FAILURE_THRESHOLD", "0"), ("LLM_FAILURE_COOLDOWN_SECONDS", "-5")):
        monkeypatch.setenv(name, value)
        with pytest.raises(ValidationError, match=name.lower()):
            Settings(_env_file=None)
        monkeypatch.delenv(name)


# --- Nothing sensitive leaks ---------------------------------------------------------------


def test_errors_and_logs_never_contain_the_key(
    sdk: Callable[..., Any], caplog: pytest.LogCaptureFixture
) -> None:
    llm, _, _ = sdk([error(401, "invalid_api_key")])

    with caplog.at_level(logging.DEBUG), pytest.raises(LLMRequestError) as caught:
        call(llm)
    with caplog.at_level(logging.DEBUG), pytest.raises(LLMUnavailableError) as paused:
        call(llm)

    for text in (str(caught.value), str(paused.value), caplog.text, repr(llm)):
        assert KEY.get_secret_value() not in text
    assert "category=authentication" in caplog.text
    assert "LLM circuit open for 60 s after authentication" in caplog.text


# --- End to end: safe fallbacks and Stage 1-4 availability --------------------------------


def reply_json() -> str:
    return json.dumps(
        {
            "status": "answered",
            "answer": "Choose Forgot Password on the sign-in page and follow the emailed link.",
            "source_faq_ids": ["faq_107"],
            "reason": "r",
        }
    )


@pytest.fixture
def app_with(
    make_settings: Callable[..., Settings], faq_service: FAQService, sdk: Callable[..., Any]
) -> Iterator[Callable[..., tuple[TestClient, Transport]]]:
    clients: list[TestClient] = []

    def _make(outcomes: list[Any], **kwargs: Any) -> tuple[TestClient, Transport]:
        llm, transport, _ = sdk(outcomes, **kwargs)
        hybrid = FakeHybrid([hybrid_spec("faq_107", similarity=0.72)])
        service = GroundedAnswerService(
            AnswerDecisionService(hybrid, FakeKeyword()),  # type: ignore[arg-type]
            ContextBuilder(),
            llm,
        )
        semantic = SemanticSearchService(  # fake model: ordinary tests never load the real one
            faq_service.list_active_faqs,
            EmbeddingService(FAKE_MODEL_NAME, loader=CountingLoader()),
        )
        app = create_app(make_settings(), faq_service=faq_service, semantic_search_service=semantic)
        app.dependency_overrides[get_grounded_answer_service] = lambda: service
        clients.append(TestClient(app))
        return clients[-1], transport

    yield _make
    for client in clients:
        client.close()


@pytest.mark.parametrize(
    "outcome",
    [
        error(401, "invalid_api_key"),
        error(429, "rate_limit_exceeded"),
        error(500),
        httpx2.ReadTimeout("t"),
        httpx2.ConnectError("c"),
        (200, ["unexpected"], None),
    ],
)
def test_every_api_failure_still_answers_with_the_approved_faq(
    app_with: Callable[..., Any], outcome: Any
) -> None:
    client, _ = app_with([outcome])

    response = client.post("/api/v1/answer", json={"query": "I forgot my password"})

    assert response.status_code == 200
    body = response.json()
    assert (body["generation_method"], body["answer"]) == ("approved_faq", FAQS["faq_107"].answer)
    for leak in (KEY.get_secret_value(), "invalid_api_key", "error with key", "Timeout"):
        assert leak not in response.text


def test_malformed_output_is_not_retried(app_with: Callable[..., Any]) -> None:
    malformed = dict(
        OK_BODY,
        output=[
            dict(
                OK_BODY["output"][0],
                content=[{"type": "output_text", "text": "not json", "annotations": []}],
            )
        ],
    )
    client, transport = app_with([(200, malformed, None)], max_retries=2)

    body = client.post("/api/v1/answer", json={"query": "q"}).json()

    assert body["generation_method"] == "approved_faq"
    assert transport.requests == 1  # one paid call, no duplicate


def test_after_a_bad_key_later_requests_do_not_call_openai(app_with: Callable[..., Any]) -> None:
    client, transport = app_with([error(401, "invalid_api_key")])

    bodies = [client.post("/api/v1/answer", json={"query": "q"}).json() for _ in range(5)]

    assert {b["generation_method"] for b in bodies} == {"approved_faq"}
    assert transport.requests == 1


def test_valid_reply_still_produces_an_llm_answer(app_with: Callable[..., Any]) -> None:
    reply_body = dict(
        OK_BODY,
        output=[
            dict(
                OK_BODY["output"][0],
                content=[{"type": "output_text", "text": reply_json(), "annotations": []}],
            )
        ],
    )
    client, _ = app_with([(200, reply_body, None)])

    assert (
        client.post("/api/v1/answer", json={"query": "q"}).json()["generation_method"]
        == "llm_grounded"
    )


def test_stage_1_to_4_endpoints_work_while_openai_is_down(app_with: Callable[..., Any]) -> None:
    client, transport = app_with([httpx2.ConnectError("down")])
    client.post("/api/v1/answer", json={"query": "q"})  # OpenAI fails

    assert client.get("/").status_code == 200
    assert client.get("/api/v1/health").json()["status"] == "healthy"
    assert client.get("/api/v1/faqs").status_code == 200
    assert client.get("/api/v1/faqs/search", params={"q": "order"}).status_code == 200
    assert (
        client.post(
            "/api/v1/hybrid-search", json={"query": "How can I track my order?"}
        ).status_code
        == 200
    )
    assert transport.requests == 1  # search never touches OpenAI


# --- Diagnostics -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("outcome", "category"),
    [
        (error(401), "authentication"),
        (error(429, "rate_limit_exceeded"), "rate_limited"),
        (httpx2.ReadTimeout("t"), "timeout"),
        (httpx2.ConnectError("c"), "network"),
        (error(404, "model_not_found"), "invalid_configuration"),
    ],
)
def test_diagnostics_record_the_error_category(
    sdk: Callable[..., Any], outcome: Any, category: str
) -> None:
    llm, _, _ = sdk([outcome])
    service = GroundedAnswerService(
        AnswerDecisionService(FakeHybrid([hybrid_spec("faq_107", similarity=0.72)]), FakeKeyword()),  # type: ignore[arg-type]
        ContextBuilder(),
        llm,
    )

    result = service.answer("I forgot my password").result

    assert result.outcome is GenerationOutcome.APPROVED_ANSWER
    assert result.diagnostics.llm_error_category == category
    assert "llm_error_category" not in result.model_dump_json()


def test_bugs_in_the_llm_step_still_return_the_approved_answer(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class BuggyLLM:
        enabled = is_available = True

        def generate(self, **kwargs: Any) -> Any:
            raise ZeroDivisionError("internal bug")

    service = GroundedAnswerService(
        AnswerDecisionService(FakeHybrid([hybrid_spec("faq_107", similarity=0.72)]), FakeKeyword()),  # type: ignore[arg-type]
        ContextBuilder(),
        BuggyLLM(),  # type: ignore[arg-type]
    )

    result = service.answer("I forgot my password").result

    assert result.outcome is GenerationOutcome.APPROVED_ANSWER
    assert result.diagnostics.fallback_reason is FallbackReason.LLM_UNAVAILABLE
    assert result.diagnostics.llm_error_category == "unexpected"
    assert "Grounded generation failed unexpectedly: ZeroDivisionError" in caplog.text
