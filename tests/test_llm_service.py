"""LLMService: lazy OpenAI client, request shape, retries, errors and secret safety.

Two kinds of test, both fully offline:
- a fake client (records calls, returns hand-made responses) for request/response logic;
- the real ``openai.OpenAI`` client over an ``httpx2.MockTransport`` for retries and error
  handling, so the SDK's actual exception mapping is exercised without any network.
"""

import json
import logging
import threading
from collections.abc import Callable, Iterator
from types import SimpleNamespace
from typing import Any

import httpx2
import openai
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from app.api.dependencies import get_llm_service
from app.core.config import Settings
from app.main import create_app
from app.services.llm_service import (
    MAX_BACKOFF_SECONDS,
    LLMNotConfiguredError,
    LLMRequestError,
    LLMResponseError,
    LLMService,
    LLMUnavailableError,
    openai_client_factory,
)

API_KEY = "sk-test-SECRET-0123456789abcdef"
INSTRUCTIONS = "Answer only from the provided FAQ text. PRIVATE-INSTRUCTIONS"
CUSTOMER_TEXT = "my email is jane.doe@example.com and my order 4711 is late"


# --- Fake client ----------------------------------------------------------------------------


def make_response(text: str = "Here is your answer.", **overrides: Any) -> SimpleNamespace:
    data = {
        "id": "resp_123",
        "model": "gpt-5.4-mini-2026-03-17",
        "status": "completed",
        "output_text": text,
        "output": [],
        "usage": SimpleNamespace(input_tokens=42, output_tokens=7),
        "incomplete_details": None,
        "_request_id": "req_abc",
    }
    data.update(overrides)
    return SimpleNamespace(**data)


class FakeClient:
    def __init__(self, responses: list[Any] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.closed = False
        self._responses = responses or [make_response()]
        self.responses = SimpleNamespace(create=self._create)

    def _create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        response = self._responses[min(len(self.calls), len(self._responses)) - 1]
        if isinstance(response, Exception):
            raise response
        return response

    def close(self) -> None:
        self.closed = True


class CountingFactory:
    def __init__(self, client: Any = None) -> None:
        self.client = client or FakeClient()
        self.keys: list[str] = []

    def __call__(self, api_key: str) -> Any:
        self.keys.append(api_key)
        return self.client


def service(
    factory: Callable[[str], Any] | None = None,
    *,
    api_key: str | None = API_KEY,
    sleeps: list[float] | None = None,
    **kwargs: Any,
) -> LLMService:
    options: dict[str, Any] = {"model": "gpt-5.4-mini", "reasoning_effort": "low"}
    options.update(kwargs)
    return LLMService(
        SecretStr(api_key) if api_key is not None else None,
        client_factory=factory or CountingFactory(),
        sleep=(sleeps.append if sleeps is not None else lambda _: None),
        **options,
    )


def generate(llm: LLMService, **kwargs: Any) -> Any:
    return llm.generate(instructions=INSTRUCTIONS, input=CUSTOMER_TEXT, **kwargs)


# --- Configuration and lazy client ----------------------------------------------------------


@pytest.mark.parametrize("api_key", [None, "", "   "])
def test_without_a_key_generation_is_unavailable_and_no_client_is_built(
    api_key: str | None,
) -> None:
    factory = CountingFactory()
    llm = service(factory, api_key=api_key)

    assert llm.is_configured is False
    with pytest.raises(LLMNotConfiguredError, match="OPENAI_API_KEY is not set"):
        generate(llm)
    assert factory.keys == []


def test_client_is_created_lazily_once_and_reused() -> None:
    factory = CountingFactory()
    llm = service(factory)
    assert llm.client_created is False  # constructing the service creates nothing

    for _ in range(5):
        generate(llm)

    assert factory.keys == [API_KEY]
    assert llm.client_created is True
    assert len(factory.client.calls) == 5


def test_concurrent_first_requests_create_one_client() -> None:
    created: list[str] = []
    barrier = threading.Barrier(8)

    def slow_factory(api_key: str) -> FakeClient:
        created.append(api_key)
        threading.Event().wait(0.05)
        return FakeClient()

    llm = service(slow_factory)

    def call() -> None:
        barrier.wait()
        generate(llm)

    threads = [threading.Thread(target=call) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert created == [API_KEY]


def test_close_releases_the_client_and_a_later_call_recreates_it() -> None:
    factory = CountingFactory()
    llm = service(factory)
    llm.close()  # nothing to close yet
    generate(llm)

    llm.close()

    assert factory.client.closed is True
    assert llm.client_created is False
    generate(llm)
    assert factory.keys == [API_KEY, API_KEY]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"model": " "},
        {"timeout_seconds": 0},
        {"timeout_seconds": 301},
        {"max_retries": -1},
        {"max_retries": 6},
        {"max_retries": True},
        {"max_output_tokens": 15},
        {"max_output_tokens": 2.5},
        {"reasoning_effort": "extreme"},
    ],
)
def test_invalid_configuration_is_rejected(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        service(**kwargs)


# --- Request shape -----------------------------------------------------------------------------


def test_request_uses_the_responses_api_with_configured_options() -> None:
    factory = CountingFactory()
    llm = service(factory, max_output_tokens=300)

    generate(llm, safety_identifier="session-hash-1")

    assert factory.client.calls == [
        {
            "model": "gpt-5.4-mini",
            "instructions": INSTRUCTIONS,
            "input": CUSTOMER_TEXT,
            "max_output_tokens": 300,
            "store": False,
            "reasoning": {"effort": "low"},
            "safety_identifier": "session-hash-1",
        }
    ]


def test_optional_parameters_are_omitted_when_not_set() -> None:
    factory = CountingFactory()
    llm = service(factory, reasoning_effort=None)

    generate(llm, max_output_tokens=50)

    [call] = factory.client.calls
    assert "reasoning" not in call and "safety_identifier" not in call
    assert call["max_output_tokens"] == 50


def test_message_list_input_is_passed_through() -> None:
    factory = CountingFactory()
    messages = [
        {"role": "user", "content": "Where is my order?"},
        {"role": "assistant", "content": "Could you share more detail?"},
        {"role": "user", "content": "It was placed on Monday."},
    ]

    service(factory).generate(instructions=INSTRUCTIONS, input=iter(messages))

    assert factory.client.calls[0]["input"] == messages


@pytest.mark.parametrize(
    ("instructions", "input"),
    [("", "q"), ("   ", "q"), ("i", ""), ("i", "  "), ("i", []), (None, "q")],
)
def test_empty_instructions_or_input_are_rejected_before_any_call(
    instructions: Any, input: Any
) -> None:
    factory = CountingFactory()

    with pytest.raises(ValueError):
        service(factory).generate(instructions=instructions, input=input)
    assert factory.keys == []


# --- Response handling -------------------------------------------------------------------------


def test_successful_response_is_returned_with_metadata() -> None:
    factory = CountingFactory(FakeClient([make_response("  Your parcel ships in 2 days.  ")]))

    result = generate(service(factory))

    assert result.text == "Your parcel ships in 2 days."
    assert (result.model, result.response_id, result.request_id) == (
        "gpt-5.4-mini-2026-03-17",
        "resp_123",
        "req_abc",
    )
    assert (result.input_tokens, result.output_tokens, result.attempts) == (42, 7, 1)
    assert result.latency_ms >= 0


@pytest.mark.parametrize(
    ("response", "reason"),
    [
        (
            make_response(
                "",
                status="incomplete",
                incomplete_details=SimpleNamespace(reason="max_output_tokens"),
            ),
            "incomplete:max_output_tokens",
        ),
        (
            make_response("partial", status="incomplete", incomplete_details=None),
            "incomplete:unknown",
        ),
        (make_response("", status="failed"), "failed"),
        (make_response("   "), "empty"),
        (
            make_response(
                "",
                output=[
                    SimpleNamespace(
                        type="message",
                        content=[
                            SimpleNamespace(type="refusal", refusal="I can't help with that.")
                        ],
                    )
                ],
            ),
            "refusal",
        ),
    ],
)
def test_unusable_responses_raise_a_response_error(response: Any, reason: str) -> None:
    llm = service(CountingFactory(FakeClient([response])))

    with pytest.raises(LLMResponseError) as caught:
        generate(llm)
    assert caught.value.reason == reason


def test_response_without_usage_or_ids_still_works() -> None:
    response = make_response(usage=None, id=None, model=None, _request_id=None)

    result = generate(service(CountingFactory(FakeClient([response]))))

    assert (result.input_tokens, result.output_tokens, result.model) == (None, None, "unknown")


# --- Real SDK over a mock transport: retries and errors ----------------------------------------

OK_BODY = {
    "id": "resp_ok",
    "object": "response",
    "created_at": 1760000000,
    "status": "completed",
    "model": "gpt-5.4-mini-2026-03-17",
    "output": [
        {
            "type": "message",
            "id": "msg_1",
            "status": "completed",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "Grounded answer.", "annotations": []}],
        }
    ],
    "usage": {
        "input_tokens": 120,
        "output_tokens": 9,
        "total_tokens": 129,
        "input_tokens_details": {"cached_tokens": 0},
        "output_tokens_details": {"reasoning_tokens": 0},
    },
}


def error_body(status: int, code: str | None = None, message: str = "error") -> dict[str, Any]:
    return {"error": {"message": message, "type": "error", "code": code, "param": None}}


class Transport:
    """Plays back scripted HTTP outcomes and records every request."""

    def __init__(self, outcomes: list[Any]) -> None:
        self.outcomes = outcomes
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        outcome = self.outcomes[min(len(self.requests), len(self.outcomes)) - 1]
        if isinstance(outcome, Exception):
            raise outcome
        status, body, headers = outcome
        return httpx2.Response(
            status, json=body, headers={"x-request-id": "req_mock", **(headers or {})}
        )


@pytest.fixture
def sdk() -> Iterator[Callable[..., tuple[LLMService, Transport, list[float]]]]:
    clients: list[openai.OpenAI] = []

    def _make(outcomes: list[Any], **kwargs: Any) -> tuple[LLMService, Transport, list[float]]:
        transport = Transport(outcomes)
        sleeps: list[float] = []

        def factory(api_key: str) -> openai.OpenAI:
            client = openai.OpenAI(
                api_key=api_key,
                max_retries=0,
                http_client=httpx2.Client(transport=httpx2.MockTransport(transport)),
            )
            clients.append(client)
            return client

        return service(factory, sleeps=sleeps, **kwargs), transport, sleeps

    yield _make
    for client in clients:
        client.close()


def test_sdk_success_sends_the_key_only_as_a_bearer_header(sdk: Callable[..., Any]) -> None:
    llm, transport, sleeps = sdk([(200, OK_BODY, None)])

    result = generate(llm)

    assert (result.text, result.request_id, result.attempts) == ("Grounded answer.", "req_mock", 1)
    assert (result.input_tokens, result.output_tokens) == (120, 9)
    [request] = transport.requests
    assert request.url.path == "/v1/responses"
    assert request.headers["authorization"] == f"Bearer {API_KEY}"
    body = json.loads(request.content)
    assert body["store"] is False
    assert body["model"] == "gpt-5.4-mini"
    assert API_KEY not in request.content.decode()
    assert sleeps == []


def test_transient_errors_are_retried_with_growing_backoff(sdk: Callable[..., Any]) -> None:
    llm, transport, sleeps = sdk(
        [(500, error_body(500), None), (503, error_body(503), None), (200, OK_BODY, None)]
    )

    result = generate(llm)

    assert result.attempts == 3 and len(transport.requests) == 3
    assert len(sleeps) == 2
    assert 0.375 <= sleeps[0] <= 0.5  # 0.5 s minus up to 25% jitter
    assert 0.75 <= sleeps[1] <= 1.0  # doubled


@pytest.mark.parametrize("status", [408, 409, 429, 500, 502, 503, 504])
def test_retries_are_bounded_then_unavailable(sdk: Callable[..., Any], status: int) -> None:
    llm, transport, sleeps = sdk([(status, error_body(status), None)], max_retries=2)

    with pytest.raises(LLMUnavailableError) as caught:
        generate(llm)

    assert len(transport.requests) == 3  # 1 attempt + 2 retries
    assert len(sleeps) == 2
    assert caught.value.status_code == status


def test_backoff_is_capped() -> None:
    from app.services.llm_service import _backoff

    assert all(_backoff(attempt) <= MAX_BACKOFF_SECONDS for attempt in range(1, 20))
    assert _backoff(10) >= MAX_BACKOFF_SECONDS * 0.75


def test_zero_retries_means_one_attempt(sdk: Callable[..., Any]) -> None:
    llm, transport, sleeps = sdk([(500, error_body(500), None)], max_retries=0)

    with pytest.raises(LLMUnavailableError):
        generate(llm)
    assert (len(transport.requests), sleeps) == (1, [])


def test_short_retry_after_is_honoured(sdk: Callable[..., Any]) -> None:
    llm, transport, sleeps = sdk(
        [
            (429, error_body(429, "rate_limit_exceeded"), {"retry-after-ms": "250"}),
            (200, OK_BODY, None),
        ]
    )

    assert generate(llm).attempts == 2
    assert sleeps == [0.25]


def test_long_retry_after_fails_fast_instead_of_waiting(sdk: Callable[..., Any]) -> None:
    llm, transport, sleeps = sdk([(429, error_body(429), {"retry-after": "60"})])

    with pytest.raises(LLMUnavailableError) as caught:
        generate(llm)

    assert len(transport.requests) == 1 and sleeps == []
    assert caught.value.retry_after_seconds == 60


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (400, "invalid_request_error"),
        (401, "invalid_api_key"),
        (403, None),
        (404, "model_not_found"),
        (422, None),
        (429, "insufficient_quota"),  # out of credit: retrying cannot help
    ],
)
def test_non_transient_errors_are_not_retried(
    sdk: Callable[..., Any], status: int, code: str | None
) -> None:
    llm, transport, sleeps = sdk([(status, error_body(status, code), None)])

    with pytest.raises(LLMRequestError) as caught:
        generate(llm)

    assert len(transport.requests) == 1 and sleeps == []
    assert (caught.value.status_code, caught.value.code) == (status, code)


@pytest.mark.parametrize(
    "error",
    [httpx2.ReadTimeout("timed out"), httpx2.ConnectError("connection refused")],
)
def test_timeouts_and_connection_errors_are_retried(
    sdk: Callable[..., Any], error: Exception
) -> None:
    llm, transport, sleeps = sdk([error, (200, OK_BODY, None)])

    assert generate(llm).attempts == 2
    assert len(sleeps) == 1


def test_persistent_timeouts_end_as_unavailable(sdk: Callable[..., Any]) -> None:
    llm, transport, _ = sdk([httpx2.ReadTimeout("timed out")])

    with pytest.raises(LLMUnavailableError, match="APITimeoutError"):
        generate(llm)
    assert len(transport.requests) == 3


# --- Secrets and customer text never leak ------------------------------------------------------


def test_invalid_key_error_never_exposes_the_key(
    sdk: Callable[..., Any], caplog: pytest.LogCaptureFixture
) -> None:
    # The real API echoes a partially masked key in its 401 message.
    message = f"Incorrect API key provided: {API_KEY[:8]}***{API_KEY[-4:]}. {API_KEY}"
    llm, _, _ = sdk([(401, error_body(401, "invalid_api_key", message), None)])

    with caplog.at_level(logging.DEBUG), pytest.raises(LLMRequestError) as caught:
        generate(llm)

    for text in (str(caught.value), repr(caught.value), repr(llm), caplog.text):
        assert API_KEY not in text
        assert API_KEY[-4:] not in text
    assert caught.value.__cause__ is None  # the SDK exception (with its message) is dropped


def test_logs_contain_metadata_but_never_prompts_or_replies(
    sdk: Callable[..., Any], caplog: pytest.LogCaptureFixture
) -> None:
    llm, _, _ = sdk([(500, error_body(500), None), (200, OK_BODY, None)])

    with caplog.at_level(logging.DEBUG):
        generate(llm)

    assert "LLM response: model=gpt-5.4-mini-2026-03-17 attempts=2" in caplog.text
    assert "request_id=req_mock" in caplog.text
    for secret in (API_KEY, "PRIVATE-INSTRUCTIONS", "jane.doe", "4711", "Grounded answer."):
        assert secret not in caplog.text


def test_repr_shows_configuration_without_the_key() -> None:
    text = repr(service())

    assert "gpt-5.4-mini" in text and "configured=True" in text
    assert API_KEY not in text


# --- Default OpenAI client --------------------------------------------------------------------


def test_default_factory_builds_an_openai_client_with_our_timeout_and_no_sdk_retries() -> None:
    client = openai_client_factory(12.0)(API_KEY)
    try:
        assert isinstance(client, openai.OpenAI)
        assert client.max_retries == 0
        assert client.timeout.read == 12.0 and client.timeout.connect == 5.0
        assert openai_client_factory(2.0)(API_KEY).timeout.connect == 2.0
    finally:
        client.close()


# --- Settings and application wiring ---------------------------------------------------------


def test_openai_settings_defaults(make_settings: Callable[..., Settings]) -> None:
    settings = make_settings()

    assert settings.openai_api_key is None
    assert (
        settings.openai_model,
        settings.openai_timeout_seconds,
        settings.openai_max_retries,
        settings.openai_max_output_tokens,
        settings.openai_reasoning_effort,
    ) == ("gpt-5.4-mini", 20.0, 2, 800, "low")


def test_openai_settings_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in {
        "OPENAI_API_KEY": API_KEY,
        "OPENAI_MODEL": " gpt-4.1-mini ",
        "OPENAI_TIMEOUT_SECONDS": "8.5",
        "OPENAI_MAX_RETRIES": "0",
        "OPENAI_MAX_OUTPUT_TOKENS": "400",
        "OPENAI_REASONING_EFFORT": "",
    }.items():
        monkeypatch.setenv(name, value)

    settings = Settings(_env_file=None)

    assert settings.openai_api_key.get_secret_value() == API_KEY  # type: ignore[union-attr]
    assert settings.openai_model == "gpt-4.1-mini"
    assert settings.openai_reasoning_effort is None  # empty = do not send
    summary = settings.safe_summary()
    assert summary["openai_api_key_set"] is True and summary["openai_model"] == "gpt-4.1-mini"
    assert API_KEY not in repr(settings) and API_KEY not in str(summary)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("OPENAI_MODEL", ""),
        ("OPENAI_TIMEOUT_SECONDS", "0"),
        ("OPENAI_TIMEOUT_SECONDS", "301"),
        ("OPENAI_MAX_RETRIES", "6"),
        ("OPENAI_MAX_OUTPUT_TOKENS", "10"),
        ("OPENAI_REASONING_EFFORT", "turbo"),
    ],
)
def test_invalid_openai_settings_fail_fast(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_app_builds_the_service_from_settings_without_creating_a_client(
    make_settings: Callable[..., Settings],
) -> None:
    app = create_app(make_settings(openai_api_key=API_KEY, openai_model="gpt-4.1-mini"))

    llm = app.state.llm_service
    assert isinstance(llm, LLMService)
    assert (llm.model, llm.is_configured, llm.client_created) == ("gpt-4.1-mini", True, False)

    with TestClient(app) as client:  # startup and shutdown with no OpenAI call
        assert client.get("/api/v1/health").status_code == 200
    assert llm.client_created is False


def test_app_starts_and_serves_search_without_any_key(
    make_settings: Callable[..., Settings],
) -> None:
    app = create_app(make_settings())

    assert app.state.llm_service.is_configured is False
    with TestClient(app) as client:
        assert client.get("/api/v1/faqs").status_code == 200


def test_dependency_returns_the_app_service_and_shutdown_closes_it(
    make_settings: Callable[..., Settings],
) -> None:
    factory = CountingFactory()
    llm = service(factory)
    app = create_app(make_settings(), llm_service=llm)
    generate(llm)

    with TestClient(app):
        assert app.state.llm_service is llm
        assert get_llm_service(SimpleNamespace(app=app)) is llm  # type: ignore[arg-type]

    assert factory.client.closed is True  # closed by the lifespan shutdown


def test_dependency_fails_loudly_without_create_app() -> None:
    bare = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))

    with pytest.raises(RuntimeError, match="LLMService is not initialised"):
        get_llm_service(bare)  # type: ignore[arg-type]


def test_unparseable_retry_after_falls_back_to_backoff(sdk: Callable[..., Any]) -> None:
    llm, _, sleeps = sdk([(503, error_body(503), {"retry-after": "soon"}), (200, OK_BODY, None)])

    assert generate(llm).attempts == 2
    assert 0.375 <= sleeps[0] <= 0.5
